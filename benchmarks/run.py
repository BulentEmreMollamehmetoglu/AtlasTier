"""Seeded HTTP workloads. Embedded comparison uses isolated real ASGI apps.

No published result is assumed: JSON reports the latencies actually observed.
"""

import argparse
import asyncio
import json
import math
import random
import statistics
import tempfile
import time
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

from app.api import create_app
from app.config import Settings
from tiering.manager import TierManager
from tiering.metadata import Tier


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def percentage(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed < 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100, exclusive")
    return parsed


def percentile(samples: list[float], percent: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def generate_trace(requests: int, key_space: int, pattern: str, hot_key_percent: float,
                   hot_access_percent: float, seed: int) -> list[str]:
    rng = random.Random(seed)
    hot_count = min(key_space - 1, max(1, math.ceil(key_space * hot_key_percent / 100)))
    result = []
    for _ in range(requests):
        if pattern == "uniform" or key_space == 1:
            index = rng.randrange(key_space)
        elif rng.random() < hot_access_percent / 100:
            index = rng.randrange(hot_count)
        else:
            index = rng.randrange(hot_count, key_space)
        result.append(f"bench-{index}")
    return result


def metric_values(payload: str) -> dict[str, float]:
    return {sample.name: sample.value
            for family in text_string_to_metric_families(payload)
            for sample in family.samples if not sample.labels}


async def seed(client: httpx.AsyncClient, key_space: int, object_bytes: int) -> None:
    for index in range(key_space):
        response = await client.put(f"/objects/bench-{index}", json={"value": "x" * object_bytes})
        response.raise_for_status()


async def workload(client: httpx.AsyncClient, trace: list[str], concurrency: int) -> dict:
    next_index = 0
    latencies: list[float] = []
    hits = {tier.value: 0 for tier in Tier}
    error_counts: dict[str, int] = {}

    async def worker():
        nonlocal next_index
        while next_index < len(trace):
            key = trace[next_index]
            next_index += 1
            started = time.perf_counter()
            try:
                response = await client.get(f"/objects/{key}")
                response.raise_for_status()
                body = response.json()
                hits[body["tier"]] += 1
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                label = (f"http_{exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
                         else type(exc).__name__)
                error_counts[label] = error_counts.get(label, 0) + 1
            finally:
                latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(min(concurrency, len(trace)))))
    elapsed = time.perf_counter() - started
    successes = sum(hits.values())
    return {
        "requests": len(trace), "successful_requests": successes,
        "errors": len(trace) - successes, "error_counts": error_counts,
        "elapsed_seconds": elapsed,
        "throughput_requests_per_second": successes / elapsed if elapsed else 0,
        "mean_latency_ms": statistics.mean(latencies) if latencies else 0,
        "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95),
        "p99_latency_ms": percentile(latencies, 99),
        "tier_hits": hits,
    }


async def measure(client: httpx.AsyncClient, trace: list[str], args) -> dict:
    if args.warmup_requests:
        warmup = generate_trace(args.warmup_requests, args.key_space, args.pattern,
                                args.hot_key_percent, args.hot_access_percent, args.seed + 1)
        warmup_result = await workload(client, warmup, args.concurrency)
        if warmup_result["errors"]:
            raise RuntimeError("warmup failed; refusing to report a misleading benchmark")
    before_response = await client.get("/metrics")
    before_response.raise_for_status()
    before = metric_values(before_response.text)
    result = await workload(client, trace, args.concurrency)
    after_response = await client.get("/metrics")
    after_response.raise_for_status()
    after = metric_values(after_response.text)
    for output, metric in (("promotions", "promotions_total"), ("demotions", "demotions_total"),
                           ("migration_failures", "migration_failures_total")):
        result[output] = int(after.get(metric, 0) - before.get(metric, 0))
    return result


async def embedded(mode: str, trace: list[str], args) -> dict:
    with tempfile.TemporaryDirectory(prefix="atlastier-bench-") as directory:
        settings = Settings(
            data_dir=Path(directory), tiering_enabled=mode == "adaptive",
            hot_capacity_bytes=args.hot_capacity_bytes, warm_capacity_bytes=args.warm_capacity_bytes,
            hot_latency_ms=args.hot_latency_ms, warm_latency_ms=args.warm_latency_ms,
            cold_latency_ms=args.cold_latency_ms,
            hot_idle_seconds=3600, warm_idle_seconds=7200,
        )
        manager = TierManager(settings)
        application = create_app(settings, manager)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://atlastier", timeout=args.timeout) as client:
            # Seed both cases quickly, then restore configured latency before measurement.
            delays = {tier: storage.latency_ms for tier, storage in manager.tiers.items()}
            for storage in manager.tiers.values():
                storage.latency_ms = 0
            try:
                await seed(client, args.key_space, args.object_bytes)
                for index in range(args.key_space):
                    await manager.migrate(f"bench-{index}", Tier.COLD)
            finally:
                for tier, storage in manager.tiers.items():
                    storage.latency_ms = delays[tier]
            # Both start with all objects in COLD. Background work is disabled for
            # the controlled comparison; GET promotions and capacity demotions run.
            return await measure(client, trace, args)


async def run(args) -> dict:
    trace = generate_trace(args.requests, args.key_space, args.pattern,
                           args.hot_key_percent, args.hot_access_percent, args.seed)
    configuration = {name: getattr(args, name) for name in (
        "requests", "concurrency", "key_space", "pattern", "hot_key_percent", "hot_access_percent",
        "seed", "object_bytes", "warmup_requests", "hot_capacity_bytes", "warm_capacity_bytes",
        "hot_latency_ms", "warm_latency_ms", "cold_latency_ms")}
    if args.url:
        async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=args.timeout) as client:
            health = await client.get("/healthz")
            health.raise_for_status()
            if health.json()["tiering_enabled"] != (args.mode == "adaptive"):
                raise RuntimeError("server tiering mode does not match --mode")
            # Remote mode uses the service's capacity/latency settings, not CLI overrides.
            await seed(client, args.key_space, args.object_bytes)
            return {"execution": "remote-http", "url": args.url, "configuration": configuration,
                    "service_configuration": "server-controlled; CLI capacity/latency ignored",
                    args.mode: await measure(client, trace, args)}
    modes = ("cold-only", "adaptive") if args.mode == "compare" else (args.mode,)
    result = {"execution": "embedded-asgi", "initial_placement": "all-cold",
              "background_tiering": False, "configuration": configuration}
    for mode in modes:
        result[mode] = await embedded(mode, trace, args)
    if args.mode == "compare":
        baseline = result["cold-only"]["mean_latency_ms"]
        adaptive = result["adaptive"]["mean_latency_ms"]
        result["comparison"] = {
            "adaptive_mean_latency_improvement_percent": (baseline - adaptive) / baseline * 100
            if baseline else 0,
            "note": "Negative improvement means adaptive was slower. Errors must be considered.",
        }
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("compare", "adaptive", "cold-only"), default="compare")
    result.add_argument("--pattern", choices=("uniform", "hotspot"), default="hotspot")
    result.add_argument("--requests", type=positive_int, default=1000)
    result.add_argument("--concurrency", type=positive_int, default=10)
    result.add_argument("--key-space", type=positive_int, default=1000)
    result.add_argument("--hot-key-percent", type=percentage, default=20)
    result.add_argument("--hot-access-percent", type=percentage, default=80)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--object-bytes", type=positive_int, default=4096)
    result.add_argument("--warmup-requests", type=int, default=0)
    result.add_argument("--hot-capacity-bytes", type=positive_int, default=1_048_576)
    result.add_argument("--warm-capacity-bytes", type=positive_int, default=10_485_760)
    for tier, default in (("hot", 1), ("warm", 15), ("cold", 80)):
        result.add_argument(f"--{tier}-latency-ms", type=float, default=default)
    result.add_argument("--timeout", type=float, default=120)
    result.add_argument("--url", help="Existing service; seeds/overwrites bench-* keys. No compare mode.")
    result.add_argument("--output", type=Path, help="Also save the actual JSON report")
    return result


def main():
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.url and args.mode == "compare":
        argument_parser.error("--url requires --mode adaptive or --mode cold-only; compare uses isolated apps")
    if args.warmup_requests < 0 or args.timeout <= 0:
        argument_parser.error("warmup must be nonnegative and timeout must be positive")
    if any(getattr(args, f"{tier}_latency_ms") < 0 for tier in ("hot", "warm", "cold")):
        argument_parser.error("latencies must be nonnegative")
    result = asyncio.run(run(args))
    payload = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if any(result.get(mode, {}).get("errors", 0) for mode in ("cold-only", "adaptive")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
