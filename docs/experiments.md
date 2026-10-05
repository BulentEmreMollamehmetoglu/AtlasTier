# Experiments and verification

Executed on 2026-10-03, using Windows, Python 3.12.14, and the packages now pinned
in `requirements.lock.txt`. These are educational experiments with injected delays,
not hardware/storage-product measurements.

## Reproduction

From the repository with dependencies installed:

```bash
python -m benchmarks.run --mode compare --pattern hotspot --requests 1000 --concurrency 4 --key-space 40 --hot-capacity-bytes 32768 --warm-capacity-bytes 131072 --seed 42 --output benchmark-results/hotspot.json
python -m benchmarks.run --mode compare --pattern uniform --requests 1000 --concurrency 4 --key-space 40 --hot-capacity-bytes 32768 --warm-capacity-bytes 131072 --seed 42 --output benchmark-results/uniform.json
```

Reports from the executed runs are retained as `results/hotspot.json` and
`results/uniform.json`. Both scenarios start all 40 keys in COLD, with 4 KiB payloads,
32 KiB HOT, 128 KiB WARM, unlimited COLD, and simulated delays of 1/15/80 ms. Warmup
is zero; setup is excluded. Hotspot uses 20% of keys for approximately 80% of reads.
The same seed produces the same requested key trace for both modes; concurrency can
change completion order and subsequent placement timing.

## Actual observed results

| Pattern / mode | Successful / attempted | Mean ms | p95 ms | p99 ms | Successful req/s |
|---|---|---:|---:|---:|---:|
| Hotspot COLD-only | 1000 / 1000 | 97.11 | 169.97 | 232.07 | 41.02 |
| Hotspot adaptive | 1000 / 1000 | 37.99 | 276.93 | 517.86 | 105.27 |
| Uniform COLD-only | 1000 / 1000 | 88.74 | 97.87 | 171.03 | 45.07 |
| Uniform adaptive | 1000 / 1000 | 63.86 | 379.98 | 529.54 | 62.46 |

Both adaptive runs improved mean latency, while p95/p99 worsened. Hotspot adaptive
served 652 HOT, 257 WARM, and 91 COLD reads, with 109 promotions and 62 demotions.
Uniform adaptive served 186 HOT, 700 WARM, and 114 COLD reads, with 266 promotions
and 219 demotions. Neither reported a request error or migration failure.

These are single runs, without confidence intervals. The two workload processes
overlapped on the same desktop, and other desktop activity can affect results.
Their COLD-only mean difference illustrates scheduling/host noise. Use repeated,
sequential runs on a quieter host for performance conclusions. There is no network
transport in embedded ASGI mode. Artificial waits dominate some operations, while
actual filesystem IO, lock contention, and Python scheduling also contribute.

Promotion requires extra source reads and writes/deletes. The small HOT tier creates
more churn under uniform access. The data therefore supports an average-latency
improvement for these specific traces and a tail-latency penalty, rather than a
general claim that adaptive tiering always wins.

## Verification evidence

- `python -m pytest -q`: **44 passed** on Windows/Python 3.12.14.
- `docker compose exec -T atlastier python -m pytest -q`: **44 passed** in Linux,
  Python 3.12.15, with no warnings after the Docker directory ownership fix.
- Failure demonstrations are included in the default suite: destination-write
  failure, source-cleanup retry, cancellation, restart, promotion failure,
  background retry, and committed overwrite cleanup failure. The first four were
  also executed separately with `python -m pytest tests/failure_demo.py -v -s`.
  The final `docker compose exec -T atlastier python -m pytest tests/failure_demo.py -q`
  run passed all **7 demonstrations** separately.
- `python -m pip check`: no broken requirements found.
- `docker compose config --quiet`: passed.
- Both comparison commands above completed with exit code 0 and their actual JSON
  reports are preserved.

- `docker compose up --build -d`: built the pinned-dependency image and started
  successfully with the named volume and a non-root service user.
- `docker compose ps`: the service became **healthy**, exposing port 8000.
- `python tests/smoke_http.py --url http://localhost:8000`: passed real HTTP
  PUT/GET/metadata/DELETE checks through HOT (5 bytes), WARM (1,048,577 bytes), and
  COLD (10,485,761 bytes), plus capacity/metrics checks. All smoke objects were deleted.

Docker Desktop initially had no running engine; after starting it, the build and
runtime checks succeeded. Sandbox restrictions required running filesystem/network
verification with normal host permissions. The first Linux test run exposed only
a pytest cache-directory permission warning; the Dockerfile now grants the service
user ownership of the work directory as well as its data directory.

## Further experiments

1. Shrink HOT below the hotspot working set and count migration churn.
2. Increase concurrency to expose the cost of serialized placement IO.
3. Compare no warmup against `--warmup-requests 1000` with the same seed.
4. Shorten the promotion window and use a sparse key trace.
5. Set realistic object sizes while remembering capacity excludes interpreter and
   filesystem overhead.
6. Start separate adaptive and cold-only servers for network measurements; keep
   loading, timing, background settings, and initial placement comparable.

Preserve JSON configuration and errors with every result. A negative improvement
is a valid result; do not omit it or replace it with predicted numbers.
