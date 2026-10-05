from dataclasses import dataclass
from typing import Protocol

from tiering.metadata import ObjectMetadata, Tier


class TieringPolicy(Protocol):
    def record_access(self, metadata: ObjectMetadata, current_time: float) -> None: ...
    def target_tier(self, metadata: ObjectMetadata, current_time: float) -> Tier: ...


@dataclass
class FrequencyRecencyPolicy:
    window_seconds: float = 30
    cold_accesses: int = 2
    warm_accesses: int = 3
    hot_idle_seconds: float = 60
    warm_idle_seconds: float = 300

    def _prune(self, metadata: ObjectMetadata, now: float) -> None:
        while metadata.recent_accesses and metadata.recent_accesses[0] < now - self.window_seconds:
            metadata.recent_accesses.popleft()

    def record_access(self, metadata: ObjectMetadata, current_time: float) -> None:
        self._prune(metadata, current_time)
        metadata.recent_accesses.append(current_time)
        # Only threshold-sized history is needed, even for an extremely hot object.
        limit = max(self.cold_accesses, self.warm_accesses)
        while len(metadata.recent_accesses) > limit:
            metadata.recent_accesses.popleft()

    def target_tier(self, metadata: ObjectMetadata, current_time: float) -> Tier:
        self._prune(metadata, current_time)
        idle = current_time - metadata.last_access_monotonic
        if metadata.current_tier == Tier.HOT and idle >= self.hot_idle_seconds:
            return Tier.WARM
        if metadata.current_tier == Tier.WARM and idle >= self.warm_idle_seconds:
            return Tier.COLD
        accesses = len(metadata.recent_accesses)
        if metadata.current_tier == Tier.COLD and accesses >= self.cold_accesses:
            return Tier.WARM
        if metadata.current_tier == Tier.WARM and accesses >= self.warm_accesses:
            return Tier.HOT
        return metadata.current_tier
