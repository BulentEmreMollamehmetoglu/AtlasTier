"""Atomic file replacement. The manager serializes mutations of each key."""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

from storage.base import StorageError


class DiskTier:
    def __init__(self, directory: Path, capacity_bytes: int | None = 10_485_760,
                 latency_ms: float = 15):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.capacity_bytes = capacity_bytes
        self.latency_ms = latency_ms
        # Files contain payloads only, never metadata or the original user key.
        self._sizes = {p.stem: p.stat().st_size for p in self.directory.glob("*.blob")}
        self.bytes_used = sum(self._sizes.values())

    @property
    def object_count(self) -> int:
        return len(self._sizes)

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".blob")

    async def get(self, key: str) -> bytes | None:
        await asyncio.sleep(self.latency_ms / 1000)
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError("disk read failed") from exc

    def _replace(self, path: Path, value: bytes) -> None:
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(value)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def put(self, key: str, value: bytes) -> None:
        await asyncio.sleep(self.latency_ms / 1000)
        path = self._path(key)
        try:
            await asyncio.to_thread(self._replace, path, value)
        except OSError as exc:
            raise StorageError("disk write failed") from exc
        self.bytes_used += len(value) - self._sizes.get(path.stem, 0)
        self._sizes[path.stem] = len(value)

    async def delete(self, key: str) -> None:
        await asyncio.sleep(self.latency_ms / 1000)
        path = self._path(key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise StorageError("disk delete failed") from exc
        self.bytes_used -= self._sizes.pop(path.stem, 0)

    async def contains(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)
