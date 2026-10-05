from typing import Protocol


class StorageError(Exception):
    """Backend IO or invariant failure; the API returns 503."""


class CapacityError(StorageError):
    """There is no space for an object in the configured hierarchy."""


class StorageTier(Protocol):
    bytes_used: int
    object_count: int
    capacity_bytes: int | None

    async def get(self, key: str) -> bytes | None: ...
    async def put(self, key: str, value: bytes) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def contains(self, key: str) -> bool: ...
