import asyncio


class MemoryTier:
    def __init__(self, capacity_bytes: int = 1_048_576, latency_ms: float = 1):
        self.capacity_bytes = capacity_bytes
        self.latency_ms = latency_ms
        self.bytes_used = 0
        self._objects: dict[str, bytes] = {}

    @property
    def object_count(self) -> int:
        return len(self._objects)

    async def get(self, key: str) -> bytes | None:
        await asyncio.sleep(self.latency_ms / 1000)
        return self._objects.get(key)

    async def put(self, key: str, value: bytes) -> None:
        await asyncio.sleep(self.latency_ms / 1000)
        old = self._objects.get(key, b"")
        self._objects[key] = value
        self.bytes_used += len(value) - len(old)

    async def delete(self, key: str) -> None:
        await asyncio.sleep(self.latency_ms / 1000)
        self.bytes_used -= len(self._objects.pop(key, b""))

    async def contains(self, key: str) -> bool:
        return key in self._objects
