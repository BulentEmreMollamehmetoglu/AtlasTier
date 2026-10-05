from tiering.metadata import ObjectMetadata, Tier, utc_now
from tiering.policy import FrequencyRecencyPolicy


def metadata(tier=Tier.COLD):
    now = utc_now()
    return ObjectMetadata("k", 1, tier, now, now, 0)


def test_frequency_threshold_and_window():
    policy = FrequencyRecencyPolicy(window_seconds=10)
    obj = metadata()
    policy.record_access(obj, 0)
    assert policy.target_tier(obj, 0) == Tier.COLD
    policy.record_access(obj, 11)
    assert policy.target_tier(obj, 11) == Tier.COLD
    policy.record_access(obj, 12)
    assert policy.target_tier(obj, 12) == Tier.WARM


def test_recency_demotion():
    policy = FrequencyRecencyPolicy()
    obj = metadata(Tier.HOT)
    assert policy.target_tier(obj, 59) == Tier.HOT
    assert policy.target_tier(obj, 60) == Tier.WARM
    obj.current_tier = Tier.WARM
    assert policy.target_tier(obj, 300) == Tier.COLD


def test_access_history_is_bounded():
    policy = FrequencyRecencyPolicy()
    obj = metadata()
    for _ in range(1000):
        policy.record_access(obj, 1)
    assert len(obj.recent_accesses) == 3
