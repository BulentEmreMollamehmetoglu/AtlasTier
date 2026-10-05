from pathlib import Path

from storage.disk import DiskTier


class ColdTier(DiskTier):
    """Local files with a larger simulated delay; this is not an S3 client."""

    def __init__(self, directory: Path, capacity_bytes: int | None = None,
                 latency_ms: float = 80):
        super().__init__(directory, capacity_bytes, latency_ms)
