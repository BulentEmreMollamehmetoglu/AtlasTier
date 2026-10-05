"""Executable failure scenarios: python -m pytest tests/failure_demo.py -v -s."""

import asyncio

import pytest

from storage.base import StorageError
from tiering.manager import TierManager
from tiering.metadata import Tier


async def test_destination_write_failure_preserves_source(manager, monkeypatch):
    await manager.put("important", b"only valid copy")
    original = await manager.get_metadata("important")

    async def failed_put(_key, _value):
        raise StorageError("injected destination write failure")

    monkeypatch.setattr(manager.tiers[Tier.WARM], "put", failed_put)
    with pytest.raises(StorageError, match="injected"):
        await manager.migrate("important", Tier.WARM)
    assert await manager.tiers[Tier.HOT].get("important") == b"only valid copy"
    assert not await manager.tiers[Tier.WARM].contains("important")
    assert await manager.get_metadata("important") == original
    assert (await manager.get("important"))["value"] == "only valid copy"
    print("Destination failure: source and metadata preserved; payload remains readable.")


async def test_source_delete_failure_keeps_destination_and_retries(manager, monkeypatch):
    await manager.put("important", b"safe")
    hot = manager.tiers[Tier.HOT]
    original_delete = hot.delete

    async def failed_delete(_key):
        raise StorageError("injected source delete failure")

    monkeypatch.setattr(hot, "delete", failed_delete)
    with pytest.raises(StorageError):
        await manager.migrate("important", Tier.WARM)
    assert manager.metadata["important"].current_tier == Tier.WARM
    assert await manager.tiers[Tier.WARM].get("important") == b"safe"
    assert await hot.contains("important")
    monkeypatch.setattr(hot, "delete", original_delete)
    await manager.evaluate()
    assert not await hot.contains("important")
    assert (await manager.stats())["warm"]["objects"] == 1
    print("Source cleanup failure: destination retained; duplicate removed on retry.")


async def test_cancellation_finishes_mutation_before_releasing_locks(manager, monkeypatch):
    await manager.put("important", b"safe")
    warm = manager.tiers[Tier.WARM]
    original_put = warm.put
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_put(key, value):
        entered.set()
        await release.wait()
        await original_put(key, value)

    monkeypatch.setattr(warm, "put", slow_put)
    task = asyncio.create_task(manager.migrate("important", Tier.WARM))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.metadata["important"].current_tier == Tier.WARM
    assert not await manager.tiers[Tier.HOT].contains("important")
    assert await warm.get("important") == b"safe"
    print("Cancellation: migration finished safely before locks were released.")


async def test_restart_is_a_new_empty_session(settings):
    first = TierManager(settings)
    await first.put("hot", b"volatile")
    await first.put("warm", b"retained file")
    await first.migrate("warm", Tier.WARM)
    await first.put("cold", b"retained file")
    await first.migrate("cold", Tier.COLD)
    old_session = first.session_dir
    await first.close()
    restarted = TierManager(settings)
    assert restarted.session_dir != old_session
    for key in ("hot", "warm", "cold"):
        with pytest.raises(KeyError):
            await restarted.get(key)
    assert sum(t.bytes_used for t in restarted.tiers.values()) == 0
    assert len(list((old_session / "warm").glob("*.blob"))) == 1
    assert len(list((old_session / "cold").glob("*.blob"))) == 1
    print("Restart: HOT/metadata lost; old WARM/COLD files retained but not recovered.")


async def test_automatic_promotion_failure_returns_read_payload(manager, monkeypatch):
    await manager.put("k", b"readable")
    await manager.migrate("k", Tier.COLD)

    async def failed_put(_key, _value):
        raise StorageError("injected promotion failure")

    monkeypatch.setattr(manager.tiers[Tier.WARM], "put", failed_put)
    await manager.get("k")
    response = await manager.get("k")
    assert response["value"] == "readable"
    assert response["tier"] == "cold"
    assert manager.metadata["k"].current_tier == Tier.COLD
    assert await manager.tiers[Tier.COLD].get("k") == b"readable"
    failures = next(sample.value for family in manager.metrics.registry.collect()
                    for sample in family.samples if sample.name == "migration_failures_total")
    assert failures == 1


async def test_background_retries_after_backend_error(settings, monkeypatch):
    settings.background_interval_seconds = .01
    manager = TierManager(settings)
    original = manager.evaluate
    calls = 0
    succeeded = asyncio.Event()

    async def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StorageError("injected cycle failure")
        await original()
        succeeded.set()

    monkeypatch.setattr(manager, "evaluate", fail_once)
    await manager.start()
    try:
        await asyncio.wait_for(succeeded.wait(), 2)
    finally:
        await manager.close()
    failures = next(sample.value for family in manager.metrics.registry.collect()
                    for sample in family.samples if sample.name == "background_failures_total")
    assert failures == 1
    assert calls >= 2


async def test_overwrite_cleanup_failure_counts_committed_write(manager, monkeypatch):
    await manager.put("k", b"old")
    await manager.migrate("k", Tier.COLD)
    cold = manager.tiers[Tier.COLD]
    original_delete = cold.delete

    async def failed_delete(_key):
        raise StorageError("injected old-copy cleanup failure")

    monkeypatch.setattr(cold, "delete", failed_delete)
    with pytest.raises(StorageError):
        await manager.put("k", b"new")
    assert (await manager.get("k"))["value"] == "new"
    writes = next(sample.value for family in manager.metrics.registry.collect()
                  for sample in family.samples if sample.name == "writes_total")
    assert writes == 2
    monkeypatch.setattr(cold, "delete", original_delete)
    await manager.put("k", b"new")
    assert not await cold.contains("k")
