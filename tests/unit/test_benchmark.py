from benchmarks.run import generate_trace, parser, percentile, run


def test_trace_reproducible_and_patterns_are_distinct():
    hotspot = generate_trace(10_000, 100, "hotspot", 20, 80, 42)
    uniform = generate_trace(10_000, 100, "uniform", 20, 80, 42)
    assert hotspot == generate_trace(10_000, 100, "hotspot", 20, 80, 42)
    hot_hits = sum(int(key.split("-")[1]) < 20 for key in hotspot)
    uniform_hits = sum(int(key.split("-")[1]) < 20 for key in uniform)
    assert 7700 < hot_hits < 8300
    assert 1700 < uniform_hits < 2300
    assert len(set(uniform)) == 100


def test_one_key_hotspot_and_percentiles():
    assert generate_trace(10, 1, "hotspot", 20, 80, 1) == ["bench-0"] * 10
    assert percentile([10, 20, 30], 50) == 20
    assert percentile([10, 20, 30], 95) == 29
    assert percentile([], 99) == 0


async def test_embedded_comparison_measures_both_real_scenarios():
    args = parser().parse_args([
        "--requests", "30", "--key-space", "2", "--concurrency", "1",
        "--hot-latency-ms", "0", "--warm-latency-ms", "0", "--cold-latency-ms", "0",
    ])
    report = await run(args)
    assert report["execution"] == "embedded-asgi"
    for mode in ("cold-only", "adaptive"):
        result = report[mode]
        assert result["successful_requests"] == result["requests"] == 30
        assert result["errors"] == result["migration_failures"] == 0
        assert sum(result["tier_hits"].values()) == 30
        assert result["throughput_requests_per_second"] > 0
        assert result["mean_latency_ms"] > 0
    assert report["cold-only"]["tier_hits"]["cold"] == 30
    assert report["cold-only"]["promotions"] == 0
    assert report["adaptive"]["promotions"] > 0
    assert report["adaptive"]["tier_hits"]["hot"] > 0
