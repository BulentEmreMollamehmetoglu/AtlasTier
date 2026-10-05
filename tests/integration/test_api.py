import asyncio

import httpx
import pytest

from app.api import create_app
from storage.base import StorageError
from tiering.metadata import Tier


@pytest.fixture
async def client(manager):
    application = create_app(manager=manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://test") as active:
        yield active


async def test_api_crud_metadata_and_tiers(client):
    assert (await client.get("/healthz")).json()["status"] == "ok"
    response = await client.put("/objects/example", json={"value": "hello"})
    assert response.status_code == 200
    assert response.json() == {"key": "example", "tier": "hot", "size_bytes": 5}
    response = await client.get("/objects/example")
    assert response.json()["value"] == "hello"
    assert response.json()["latency_ms"] >= 0
    metadata = (await client.get("/objects/example/metadata")).json()
    assert metadata["access_count"] == 1
    assert metadata["current_tier"] == "hot"
    assert metadata["promotion_count"] == metadata["demotion_count"] == 0
    assert (await client.get("/tiers")).json()["hot"]["bytes_used"] == 5
    assert (await client.delete("/objects/example")).status_code == 204
    for path in ("/objects/example", "/objects/example/metadata"):
        assert (await client.get(path)).status_code == 404
    assert (await client.delete("/objects/example")).status_code == 404


async def test_validation_and_size_limits(client, manager):
    manager.settings.max_object_bytes = 4
    assert (await client.put("/objects/k", json={"value": "12345"})).status_code == 413
    assert (await client.put("/objects/k", json={"value": 123})).status_code == 422
    assert (await client.put("/objects/k", json={"unknown": "x"})).status_code == 422
    assert (await client.put("/objects/k", json={"value": "", "extra": 1})).status_code == 422


async def test_metrics_have_actual_values_and_bounded_labels(client, manager):
    await client.put("/objects/sensitive-key", json={"value": "abc"})
    await client.get("/objects/sensitive-key")
    await client.get("/objects/missing")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for metric in ("requests_total", "reads_total", "writes_total", "tier_hits_total",
                   "promotions_total", "demotions_total", "migration_failures_total",
                   "tier_bytes", "tier_objects", "request_latency_seconds"):
        assert metric in text
    assert 'reads_total 1.0' in text
    assert 'tier_hits_total{tier="hot"} 1.0' in text
    assert 'tier_bytes{tier="hot"} 3.0' in text
    assert 'route="/objects/{key}"' in text
    assert "sensitive-key" not in text


async def test_api_backend_failure_is_503(client, manager, monkeypatch):
    async def fail(_key, _value):
        raise StorageError("injected failure")

    monkeypatch.setattr(manager.tiers[Tier.HOT], "put", fail)
    response = await client.put("/objects/x", json={"value": "x"})
    assert response.status_code == 503
    assert (await client.get("/objects/x")).status_code == 404


async def test_api_concurrent_write_read(client, manager):
    responses = await asyncio.gather(*(client.put("/objects/shared", json={"value": f"v{i}"})
                                       for i in range(20)))
    assert all(r.status_code == 200 for r in responses)
    responses = await asyncio.gather(*(client.get("/objects/shared") for _ in range(20)))
    assert all(r.status_code == 200 and r.json()["value"] == "v19" for r in responses)
    assert manager.metadata["shared"].access_count == 20
    assert (await client.get("/tiers")).json()["hot"]["objects"] == 1


async def test_lifespan_starts_and_stops_background(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        worker = application.state.manager._worker
        assert worker is not None and not worker.done()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
    assert worker.done()


async def test_reads_work_through_each_disk_tier(client, manager):
    await client.put("/objects/k", json={"value": "on disk"})
    for tier in (Tier.WARM, Tier.COLD):
        await manager.migrate("k", tier)
        response = await client.get("/objects/k")
        assert response.status_code == 200
        assert response.json()["tier"] == tier.value
        assert response.json()["value"] == "on disk"
    assert manager.metadata["k"].access_count == 2
