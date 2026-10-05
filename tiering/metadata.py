from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


ORDER = (Tier.HOT, Tier.WARM, Tier.COLD)


@dataclass
class ObjectMetadata:
    key: str
    size_bytes: int
    current_tier: Tier
    created_at: datetime
    last_accessed_at: datetime
    last_access_monotonic: float
    access_count: int = 0
    promotion_count: int = 0
    demotion_count: int = 0
    recent_accesses: deque[float] = field(default_factory=deque)

    def snapshot(self) -> dict:
        return {
            "key": self.key,
            "size_bytes": self.size_bytes,
            "current_tier": self.current_tier.value,
            "created_at": self.created_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "access_count": self.access_count,
            "promotion_count": self.promotion_count,
            "demotion_count": self.demotion_count,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
