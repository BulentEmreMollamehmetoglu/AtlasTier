import asyncio

import pytest

from storage.base import CapacityError
from tiering.manager import TierManager
from tiering.metadata import Tier


async def assert_single_ownership(manager, key):
    current = manager.metadata[key].current_tier
    for tier, backend in manager.tiers.items():
        assert await backend.contains(key) == (tier == current)


async def test_metadata_and_unicode_size(manager):
    value = "ö🍎".encode()
    result = await manager.put("fruit", value)
    assert result == {"key": "fruit", "tier": "hot", "size_bytes": len(value)}
    before = await manager.get_metadata("fruit")
    assert before["access_count"] == 0
    assert (await manager.get("fruit"))["value"] == "ö🍎"
    after = await manager.get_metadata("fruit")
    assert after["access_count"] == 1
    assert after["created_at"] == before["created_at"]
    assert after["last_accessed_at"] >= before["last_accessed_at"]
    after["access_count"] = 999
    assert manager.metadata["fruit"].access_count == 1


async def test_capacity_cascades_and_lru(settings):
    settings.hot_capacity_bytes = 6
    settings.warm_capacity_bytes = 3
    ticks = iter(range(100))
    manager = TierManager(settings, clock=lambda: next(ticks))
    await manager.put("a", b"aaa")
    await manager.put("b", b"bbb")
    await manager.get("a")
    await manager.put("c", b"ccc")
    assert manager.metadata["b"].current_tier == Tier.WARM
    await manager.put("d", b"ddd")
    assert manager.metadata["b"].current_tier == Tier.COLD
    assert manager.metadata["a"].current_tier == Tier.WARM
    stats = await manager.stats()
    assert stats["hot"]["bytes_used"] == 6
    assert stats["warm"]["bytes_used"] == 3
    assert stats["cold"]["bytes_used"] == 3
    for key in "abcd":
        await assert_single_ownership(manager, key)


async def test_promotions_require_fresh_accesses_in_each_tier(manager):
    await manager.put("a", b"payload")
    await manager.migrate("a", Tier.COLD)
    first = await manager.get("a")
    assert first["tier"] == "cold"
    assert manager.metadata["a"].current_tier == Tier.COLD
    second = await manager.get("a")
    assert second["tier"] == "cold"  # Serving tier, even though the next GET is WARM.
    assert manager.metadata["a"].current_tier == Tier.WARM
    for _ in range(2):
        await manager.get("a")
    assert manager.metadata["a"].current_tier == Tier.WARM
    await manager.get("a")
    assert manager.metadata["a"].current_tier == Tier.HOT
    assert manager.metadata["a"].promotion_count == 2
    assert manager.metadata["a"].demotion_count == 1
    await assert_single_ownership(manager, "a")


async def test_oversized_object_demotes_and_does_not_promote(settings):
    settings.hot_capacity_bytes = 2
    settings.warm_capacity_bytes = 4
    manager = TierManager(settings)
    assert (await manager.put("big", b"12345"))["tier"] == "cold"
    for _ in range(5):
        assert (await manager.get("big"))["tier"] == "cold"
    assert manager.metadata["big"].current_tier == Tier.COLD
    assert (await manager.stats())["hot"]["bytes_used"] == 0
    await assert_single_ownership(manager, "big")


async def test_overwrite_from_other_tier_and_delete(manager):
    await manager.put("a", b"old")
    await manager.migrate("a", Tier.COLD)
    created = manager.metadata["a"].created_at
    await manager.put("a", b"new data")
    assert (await manager.get("a"))["value"] == "new data"
    assert manager.metadata["a"].created_at == created
    assert sum(t.bytes_used for t in manager.tiers.values()) == 8
    await assert_single_ownership(manager, "a")
    await manager.delete("a")
    assert sum(t.object_count for t in manager.tiers.values()) == 0
    with pytest.raises(KeyError):
        await manager.get("a")


async def test_finite_capacity_rejection_preserves_existing_objects(settings):
    settings.hot_capacity_bytes = 3
    settings.warm_capacity_bytes = 3
    settings.cold_capacity_bytes = 3
    manager = TierManager(settings)
    for key in "abc":
        await manager.put(key, b"123")
    with pytest.raises(CapacityError):
        await manager.put("d", b"456")
    assert "d" not in manager.metadata
    assert set(manager.metadata) == set("abc")
    for key in "abc":
        assert (await manager.get(key))["value"] == "123"
        await assert_single_ownership(manager, key)
    for item in (await manager.stats()).values():
        assert item["bytes_used"] <= item["capacity_bytes"]


async def test_concurrent_reads_account_exactly(manager):
    await manager.put("a", b"value")
    results = await asyncio.gather(*(manager.get("a") for _ in range(50)))
    assert all(r["value"] == "value" for r in results)
    assert manager.metadata["a"].access_count == 50


async def test_concurrent_writes_no_duplicate_copies(settings):
    settings.hot_capacity_bytes = 12
    settings.warm_capacity_bytes = 12
    manager = TierManager(settings)
    writes = [(f"k{i % 5}", f"value-{i}".encode()) for i in range(40)]
    await asyncio.wait_for(asyncio.gather(*(manager.put(k, v) for k, v in writes)), 5)
    assert set(manager.metadata) == {f"k{i}" for i in range(5)}
    expected = dict(writes)
    for key, value in expected.items():
        assert (await manager.get(key))["value"] == value.decode()
        await assert_single_ownership(manager, key)
    assert sum(t.bytes_used for t in manager.tiers.values()) == sum(len(v) for v in expected.values())


async def test_different_key_reads_overlap(manager, monkeypatch):
    # Force distinct stripes, then synchronize both backend reads with an event.
    keys = next(("a", f"b{i}") for i in range(1000) if manager._lock("a") is not manager._lock(f"b{i}"))
    for key in keys:
        await manager.put(key, b"x")
    entered = 0
    both_reading = asyncio.Event()
    backend = manager.tiers[Tier.HOT]
    original = backend.get

    async def coordinated_get(key):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_reading.set()
        await asyncio.wait_for(both_reading.wait(), 1)
        return await original(key)

    monkeypatch.setattr(backend, "get", coordinated_get)
    results = await asyncio.gather(*(manager.get(key) for key in keys))
    assert len(results) == 2


async def test_cascading_migration_with_colliding_lock_stripes(settings):
    settings.hot_capacity_bytes = 3
    settings.warm_capacity_bytes = 3
    manager = TierManager(settings)
    # Worst case: every key shares one lock, including all ancestor migrations.
    lock = asyncio.Lock()
    manager._key_locks = [lock] * 256
    await asyncio.wait_for(asyncio.gather(*(manager.put(k, b"123") for k in "abcd")), 2)
    assert sum(t.object_count for t in manager.tiers.values()) == 4
    for key in "abcd":
        await assert_single_ownership(manager, key)


async def test_background_recency_and_clean_shutdown(settings):
    settings.background_interval_seconds = .01
    settings.hot_idle_seconds = .02
    settings.warm_idle_seconds = .04
    manager = TierManager(settings)
    await manager.put("idle", b"x")
    await manager.start()
    try:
        async with asyncio.timeout(2):
            while manager.metadata["idle"].current_tier != Tier.COLD:
                await asyncio.sleep(.01)
    finally:
        worker = manager._worker
        await manager.close()
    assert worker.done()
    assert manager._worker is None
    assert manager.metadata["idle"].demotion_count == 2
    await assert_single_ownership(manager, "idle")


async def test_cold_only_mode_does_not_promote(settings):
    settings.tiering_enabled = False
    manager = TierManager(settings)
    assert (await manager.put("k", b"x"))["tier"] == "cold"
    for _ in range(6):
        assert (await manager.get("k"))["tier"] == "cold"
    await manager.evaluate()
    assert manager.metadata["k"].promotion_count == 0
    await assert_single_ownership(manager, "k")


async def test_rejected_oversized_overwrite_preserves_old_value(settings):
    settings.hot_capacity_bytes = 3
    settings.warm_capacity_bytes = 3
    settings.cold_capacity_bytes = 3
    manager = TierManager(settings)
    await manager.put("k", b"old")
    before = await manager.get_metadata("k")
    with pytest.raises(CapacityError):
        await manager.put("k", b"too large")
    assert await manager.get_metadata("k") == before
    assert (await manager.get("k"))["value"] == "old"
    await assert_single_ownership(manager, "k")


async def test_empty_payload_accounted_as_an_object(manager):
    await manager.put("empty", b"")
    assert (await manager.get("empty"))["value"] == ""
    assert (await manager.stats())["hot"]["objects"] == 1
    assert (await manager.stats())["hot"]["bytes_used"] == 0
    await manager.migrate("empty", Tier.COLD)
    await assert_single_ownership(manager, "empty")
    await manager.delete("empty")
    assert sum(t.object_count for t in manager.tiers.values()) == 0


async def test_mixed_concurrent_reads_writes_migrations(manager):
    await manager.put("shared", b"initial")

    async def write_values():
        for i in range(15):
            await manager.put("shared", f"version-{i}".encode())

    async def read_values():
        for _ in range(30):
            value = (await manager.get("shared"))["value"]
            assert value == "initial" or value.startswith("version-")

    async def move_values():
        for tier in (Tier.WARM, Tier.COLD, Tier.HOT) * 5:
            await manager.migrate("shared", tier)

    await asyncio.wait_for(asyncio.gather(write_values(), read_values(), move_values()), 5)
    assert (await manager.get("shared"))["value"] == "version-14"
    assert manager.metadata["shared"].access_count == 31
    await assert_single_ownership(manager, "shared")
