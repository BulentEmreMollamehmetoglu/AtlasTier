"""Placement mutations take capacity -> key locks. Reads take only a key lock.

Reads release their key lock before requesting promotion, preventing lock inversion.
Backend capacities are enforced here, rather than by individual storage backends.
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Awaitable, TypeVar

from app.config import Settings
from observability.metrics import Metrics
from storage.base import CapacityError, StorageError, StorageTier
from storage.cold import ColdTier
from storage.disk import DiskTier
from storage.memory import MemoryTier
from tiering.metadata import ORDER, ObjectMetadata, Tier, utc_now
from tiering.policy import FrequencyRecencyPolicy, TieringPolicy

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def finish_mutation(operation: Awaitable[T]) -> T:
    """Finish in-flight IO/accounting before releasing locks, even on cancellation.

    asyncio.to_thread cannot cancel a filesystem write already running in a thread.
    Holding the locks until the operation finishes prevents late orphaned writes.
    """
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            if task.done():
                # Retrieve an exception so it never becomes an unobserved task failure.
                if not task.cancelled():
                    task.exception()
                raise
            cancelled = True


class TierManager:
    def __init__(self, settings: Settings, tiers: dict[Tier, StorageTier] | None = None,
                 policy: TieringPolicy | None = None, metrics: Metrics | None = None,
                 clock=time.monotonic):
        self.settings = settings
        # Metadata is deliberately volatile. Old sessions remain on disk for inspection.
        self.session_dir = settings.data_dir / "sessions" / uuid.uuid4().hex
        self.tiers = tiers if tiers is not None else {
            Tier.HOT: MemoryTier(settings.hot_capacity_bytes, settings.hot_latency_ms),
            Tier.WARM: DiskTier(self.session_dir / "warm", settings.warm_capacity_bytes,
                                settings.warm_latency_ms),
            Tier.COLD: ColdTier(self.session_dir / "cold", settings.cold_capacity_bytes,
                                settings.cold_latency_ms),
        }
        self.policy = policy or FrequencyRecencyPolicy(
            settings.promotion_window_seconds, settings.cold_promotion_accesses,
            settings.warm_promotion_accesses, settings.hot_idle_seconds, settings.warm_idle_seconds,
        )
        self.metrics = metrics or Metrics()
        self.metadata: dict[str, ObjectMetadata] = {}
        self._capacity_lock = asyncio.Lock()
        # Bounded lock table; colliding keys may serialize, but never grow the table.
        self._key_locks = [asyncio.Lock() for _ in range(256)]
        self._pending_cleanup: set[tuple[str, Tier]] = set()
        self._clock = clock
        self._stop = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self.metrics.sync(self.tiers)

    def _lock(self, key: str) -> asyncio.Lock:
        return self._key_locks[hash(key) % len(self._key_locks)]

    @asynccontextmanager
    async def _key_guard(self, key: str, held: tuple[asyncio.Lock, ...] = ()):
        lock = self._lock(key)
        if lock in held:
            yield held
        else:
            async with lock:
                yield (*held, lock)

    def _metadata(self, key: str) -> ObjectMetadata:
        try:
            return self.metadata[key]
        except KeyError:
            raise KeyError(key) from None

    def _fits(self, tier: Tier, size: int) -> bool:
        capacity = self.tiers[tier].capacity_bytes
        return capacity is None or size <= capacity

    def _placement(self, size: int, start: Tier = Tier.HOT) -> Tier:
        for tier in ORDER[ORDER.index(start):]:
            if self._fits(tier, size):
                return tier
        raise CapacityError("object exceeds every available tier capacity")

    async def _cleanup_key(self, key: str) -> None:
        for pending_key, tier in list(self._pending_cleanup):
            if pending_key == key:
                await self.tiers[tier].delete(key)
                self._pending_cleanup.discard((key, tier))

    async def _remove_copy(self, key: str, tier: Tier) -> None:
        # Record before IO: even a failing delete may need retry.
        self._pending_cleanup.add((key, tier))
        await self.tiers[tier].delete(key)
        self._pending_cleanup.discard((key, tier))

    async def _make_room(self, tier: Tier, additional: int, excluded: str,
                         held: tuple[asyncio.Lock, ...]) -> None:
        storage = self.tiers[tier]
        capacity = storage.capacity_bytes
        if capacity is None:
            return
        if additional > capacity:
            raise CapacityError(f"object does not fit in {tier}")
        while storage.bytes_used + additional > capacity:
            candidates = [m for m in self.metadata.values()
                          if m.current_tier == tier and m.key != excluded]
            if tier == Tier.COLD or not candidates:
                raise CapacityError(f"{tier} has insufficient capacity")
            victim = min(candidates, key=lambda m: (m.last_access_monotonic, m.key))
            async with self._key_guard(victim.key, held) as victim_lock:
                # No other placement mutation can run while capacity is locked.
                destination = self._placement(victim.size_bytes, ORDER[ORDER.index(tier) + 1])
                await self._move_locked(victim.key, destination, victim_lock)

    async def _move_locked(self, key: str, destination: Tier,
                           held: tuple[asyncio.Lock, ...]) -> None:
        metadata = self._metadata(key)
        source = metadata.current_tier
        if source == destination:
            return
        try:
            await self._cleanup_key(key)
            await self._make_room(destination, metadata.size_bytes, key, held)
            value = await self.tiers[source].get(key)
            if value is None:
                raise StorageError("metadata points to a missing source object")
            # A successful put acknowledges an atomic payload write. No source is
            # removed until this succeeds. A failed put must leave no partial payload.
            await self.tiers[destination].put(key, value)
            metadata.current_tier = destination
            metadata.recent_accesses.clear()
            if ORDER.index(destination) < ORDER.index(source):
                metadata.promotion_count += 1
                self.metrics.promotions.inc()
            else:
                metadata.demotion_count += 1
                self.metrics.demotions.inc()
            await self._remove_copy(key, source)
        except StorageError:
            self.metrics.failures.inc()
            raise
        finally:
            self.metrics.sync(self.tiers)

    async def migrate(self, key: str, destination: Tier) -> None:
        """Explicit operation, also useful for failure demonstrations."""
        async def operation():
            async with self._capacity_lock:
                async with self._key_guard(key) as held:
                    await self._move_locked(key, destination, held)
        await finish_mutation(operation())

    async def put(self, key: str, value: bytes) -> dict:
        if len(value) > self.settings.max_object_bytes:
            raise ValueError("object exceeds configured max_object_bytes")

        async def operation():
            async with self._capacity_lock:
                async with self._key_guard(key) as held:
                    await self._cleanup_key(key)
                    old = self.metadata.get(key)
                    start = Tier.HOT if self.settings.tiering_enabled else Tier.COLD
                    destination = self._placement(len(value), start)
                    reused = old.size_bytes if old and old.current_tier == destination else 0
                    # Admission/evictions happen before replacing the requested key.
                    await self._make_room(destination, len(value) - reused, key, held)
                    stage_hot = old is None and destination != Tier.HOT and self.settings.tiering_enabled
                    write_tier = Tier.HOT if stage_hot else destination
                    await self.tiers[write_tier].put(key, value)
                    now = utc_now()
                    cleanup_source = None
                    if old is None:
                        metadata = ObjectMetadata(key, len(value), write_tier, now, now, self._clock())
                        self.metadata[key] = metadata
                    else:
                        metadata = old
                        source = old.current_tier
                        metadata.current_tier = destination
                        metadata.size_bytes = len(value)
                        metadata.last_accessed_at = now
                        metadata.last_access_monotonic = self._clock()
                        metadata.recent_accesses.clear()
                        if source != destination:
                            cleanup_source = source
                    self.metrics.writes.inc()
                    if cleanup_source is not None:
                        await self._remove_copy(key, cleanup_source)
                    if stage_hot:
                        await self._move_locked(key, destination, held)
                    return {"key": key, "tier": metadata.current_tier.value,
                            "size_bytes": metadata.size_bytes}
        try:
            return await finish_mutation(operation())
        finally:
            self.metrics.sync(self.tiers)

    async def get(self, key: str) -> dict:
        started = time.perf_counter()
        async with self._key_guard(key):
            metadata = self._metadata(key)
            served_tier = metadata.current_tier
            value = await self.tiers[served_tier].get(key)
            if value is None:
                raise StorageError("metadata points to a missing object")
            now = self._clock()
            metadata.access_count += 1
            metadata.last_accessed_at = utc_now()
            metadata.last_access_monotonic = now
            self.policy.record_access(metadata, now)
            target = self.policy.target_tier(metadata, now)
            self.metrics.reads.inc()
            self.metrics.hits.labels(tier=served_tier.value).inc()
        if self.settings.tiering_enabled and ORDER.index(target) < ORDER.index(served_tier):
            # Recheck after releasing/reacquiring: a PUT/DELETE may have intervened.
            async def promote():
                async with self._capacity_lock:
                    async with self._key_guard(key) as held:
                        current = self.metadata.get(key)
                        if current is not metadata:
                            return
                        destination = self.policy.target_tier(current, self._clock())
                        if ORDER.index(destination) < ORDER.index(current.current_tier):
                            if self._fits(destination, current.size_bytes):
                                await self._move_locked(key, destination, held)
            try:
                await finish_mutation(promote())
            except StorageError:
                # The payload was already read successfully. Placement is best-effort;
                # failure is visible via metrics/logs and explicit migrate raises.
                logger.warning("Automatic promotion failed for %r", key, exc_info=True)
        return {"key": key, "value": value.decode("utf-8"), "tier": served_tier.value,
                "latency_ms": (time.perf_counter() - started) * 1000}

    async def delete(self, key: str) -> None:
        async def operation():
            async with self._capacity_lock:
                async with self._key_guard(key):
                    metadata = self._metadata(key)
                    await self._cleanup_key(key)
                    await self.tiers[metadata.current_tier].delete(key)
                    del self.metadata[key]
        try:
            await finish_mutation(operation())
        finally:
            self.metrics.sync(self.tiers)

    async def get_metadata(self, key: str) -> dict:
        async with self._key_guard(key):
            return self._metadata(key).snapshot()

    async def stats(self) -> dict:
        async with self._capacity_lock:
            return {name.value: {"objects": storage.object_count,
                                 "bytes_used": storage.bytes_used,
                                 "capacity_bytes": storage.capacity_bytes}
                    for name, storage in self.tiers.items()}

    async def evaluate(self) -> None:
        async def operation():
            async with self._capacity_lock:
                for key, _tier in list(self._pending_cleanup):
                    async with self._key_guard(key):
                        await self._cleanup_key(key)
                if self.settings.tiering_enabled:
                    # Snapshot keys because reads/placements may update metadata.
                    for key in list(self.metadata):
                        async with self._key_guard(key) as held:
                            metadata = self.metadata[key]
                            target = self.policy.target_tier(metadata, self._clock())
                            if target != metadata.current_tier and self._fits(target, metadata.size_bytes):
                                await self._move_locked(key, target, held)
                for tier in ORDER:
                    async with self._key_guard("") as held:
                        await self._make_room(tier, 0, "", held)
        try:
            await finish_mutation(operation())
        finally:
            self.metrics.sync(self.tiers)

    async def _background(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.background_interval_seconds)
            except TimeoutError:
                try:
                    await self.evaluate()
                except Exception:
                    self.metrics.background_failures.inc()
                    logger.exception("Background tiering cycle failed; retrying next interval")

    async def start(self) -> None:
        if self._worker is None:
            self._stop.clear()
            self._worker = asyncio.create_task(self._background(), name="atlastier-tiering")

    async def close(self) -> None:
        self._stop.set()
        if self._worker is not None:
            await self._worker
            self._worker = None
