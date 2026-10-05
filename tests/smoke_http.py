"""Smoke-test a default running service, including actual HTTP reads from all tiers.

Run: python tests/smoke_http.py --url http://localhost:8000
Only uniquely named smoke objects are created/deleted. Assumes default capacities
and object-size limit; run against a dedicated development service.
"""

import argparse
import asyncio
import json
import uuid

import httpx


async def smoke(url: str):
    prefix = f"smoke-{uuid.uuid4().hex}"
    created = []
    results = {}
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        health = await client.get("/healthz")
        health.raise_for_status()
        assert health.json()["tiering_enabled"] is True
        values = {"hot": "hello", "warm": "w" * (1_048_576 + 1),
                  "cold": "c" * (10_485_760 + 1)}
        try:
            for tier, value in values.items():
                key = f"{prefix}-{tier}"
                response = await client.put(f"/objects/{key}", json={"value": value})
                response.raise_for_status()
                created.append(key)
                assert response.json()["tier"] == tier
                assert response.json()["size_bytes"] == len(value.encode())
                response = await client.get(f"/objects/{key}")
                response.raise_for_status()
                assert response.json()["value"] == value
                assert response.json()["tier"] == tier
                metadata = await client.get(f"/objects/{key}/metadata")
                metadata.raise_for_status()
                assert metadata.json()["current_tier"] == tier
                assert metadata.json()["access_count"] == 1
                results[tier] = {"size_bytes": len(value), "get_latency_ms": response.json()["latency_ms"]}
            stats = await client.get("/tiers")
            stats.raise_for_status()
            for item in stats.json().values():
                assert item["capacity_bytes"] is None or item["bytes_used"] <= item["capacity_bytes"]
            metrics = await client.get("/metrics")
            metrics.raise_for_status()
            assert "tier_hits_total" in metrics.text
            assert prefix not in metrics.text
        finally:
            for key in created:
                response = await client.delete(f"/objects/{key}")
                response.raise_for_status()
                assert response.status_code == 204
                assert (await client.get(f"/objects/{key}")).status_code == 404
    return {"status": "passed", "url": url, "tiers": results, "cleanup": "completed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000")
    print(json.dumps(asyncio.run(smoke(parser.parse_args().url)), indent=2))
