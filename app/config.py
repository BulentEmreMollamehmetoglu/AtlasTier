"""Configuration is read once at startup; all delays are simulated milliseconds."""

import os
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    hot_capacity_bytes: int = Field(default=1_048_576, gt=0)
    warm_capacity_bytes: int = Field(default=10_485_760, gt=0)
    cold_capacity_bytes: int | None = Field(default=None, gt=0)
    hot_latency_ms: float = Field(default=1, ge=0)
    warm_latency_ms: float = Field(default=15, ge=0)
    cold_latency_ms: float = Field(default=80, ge=0)
    promotion_window_seconds: float = Field(default=30, gt=0)
    cold_promotion_accesses: int = Field(default=2, gt=0)
    warm_promotion_accesses: int = Field(default=3, gt=0)
    hot_idle_seconds: float = Field(default=60, gt=0)
    warm_idle_seconds: float = Field(default=300, gt=0)
    background_interval_seconds: float = Field(default=2, gt=0)
    tiering_enabled: bool = True
    data_dir: Path = Path("data")
    max_object_bytes: int = Field(default=16_777_216, gt=0)

    @model_validator(mode="after")
    def ordered_idle_thresholds(self):
        if self.warm_idle_seconds < self.hot_idle_seconds:
            raise ValueError("warm_idle_seconds must be at least hot_idle_seconds")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for name in cls.model_fields:
            raw = os.getenv(f"ATLASTIER_{name.upper()}")
            if raw is not None:
                values[name] = None if name == "cold_capacity_bytes" and raw.lower() == "none" else raw
        return cls(**values)
