import pytest

from app.config import Settings
from tiering.manager import TierManager


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, hot_latency_ms=0, warm_latency_ms=0, cold_latency_ms=0)


@pytest.fixture
def manager(settings):
    return TierManager(settings)
