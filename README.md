# AtlasTier

AtlasTier is an educational adaptive tiered storage system for exploring storage
hierarchies and data placement in data-intensive and AI/ML systems. It stores UTF-8
string objects, promotes frequently read objects, and demotes less recently used
objects to make space. It runs as one Python process with no database or cloud service.

Correctness, explicit failure behavior, and reproducible experiments are the focus.
This is a learning project, not a production storage service.

## Run

Install Docker with a running Linux container engine, then:

```bash
docker compose up --build
```

The API is at <http://localhost:8000>; interactive API documentation is at
<http://localhost:8000/docs>. Compose persists both disk tiers under the `tier-data`
volume. A restart starts a new empty session: retained files are **not** recovered.

For local development, use Python **3.12 or newer**:

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell instead:
# .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1
```

Always use one worker. Each process has its own memory tier, metadata, locks,
metrics, and session directory. Multiple workers do not form a shared storage service.
`requirements.lock.txt` pins the verified environment; `requirements.txt` lists the
supported direct dependency ranges for intentional upgrades.

## Architecture

```text
Client -> FastAPI -> TierManager -> HOT  : bytes in memory
                        |       -> WARM : atomic local files
                        |       -> COLD : atomic local files + longer simulated delay
                        |
                        +-> frequency/recency policy
                        +-> metadata + per-key locks + placement lock
                        +-> Prometheus metrics
                        +<- lifespan-managed background worker
```

The API validates requests and maps errors. All placement decisions live in
`tiering/manager.py`; the replaceable policy lives in `tiering/policy.py`.

| Tier | Backend | Default payload capacity | Simulated operation latency |
|---|---|---|---|
| HOT | Memory | 1 MiB | 1 ms |
| WARM | Local files | 10 MiB | 15 ms |
| COLD | Separate local files | Unlimited | 80 ms |

These delays are `asyncio.sleep()` values applied to each get/put/delete, **not
measurements of real hardware**. Disk IO and request scheduling add actual overhead.
Migration incurs source get, destination put, and source delete delays, so a read
that triggers promotion can take much longer than its serving tier's nominal delay.
`contains` is a diagnostic existence check and does not inject latency.

## API

Keys are single URL path segments of 1–512 characters. Use URL encoding for special
characters; slash-containing keys are not supported by the HTTP API. File names are
SHA-256 hashes of keys, so a key cannot escape a disk directory.

| Method and path | Behavior |
|---|---|
| `PUT /objects/{key}` | JSON `{"value":"example data"}`; returns key, final tier, UTF-8 byte size |
| `GET /objects/{key}` | Returns key, value, serving tier, manager latency in ms; updates access metadata |
| `DELETE /objects/{key}` | Deletes ownership and any pending duplicate; returns 204 |
| `GET /objects/{key}/metadata` | Tier, byte size, access count, UTC timestamps, promotion/demotion counts |
| `GET /tiers` | Physical object count, bytes used, capacity per tier; unlimited capacity is `null` |
| `GET /metrics` | Prometheus text format |
| `GET /healthz` | Basic liveness and tiering mode |

```bash
curl -X PUT http://localhost:8000/objects/example -H 'Content-Type: application/json' -d '{"value":"example data"}'
curl http://localhost:8000/objects/example
curl http://localhost:8000/objects/example/metadata
curl http://localhost:8000/tiers
curl -X DELETE http://localhost:8000/objects/example
```

PowerShell equivalent for storing an object:

```powershell
Invoke-RestMethod -Method Put -Uri http://localhost:8000/objects/example -ContentType application/json -Body '{"value":"example data"}'
```

Missing objects return 404, invalid JSON/schema returns 422, values over the configured
object limit return 413, unavailable capacity returns 507, and backend failures return
503. Empty strings are valid objects. Object size counts UTF-8 payload bytes only,
excluding Python objects, metadata, filesystem overhead, and temporary copies. The
size limit is checked after JSON parsing; it is not a streaming request-body limit.

## Promotion, demotion, and capacity

New adaptive objects enter HOT. If an object itself exceeds HOT capacity, it is
immediately demoted to the first slower tier that can hold it. The PUT response
shows the final placement. The cold-only mode writes directly to COLD.

Each successful GET records a monotonic timestamp in a bounded access history:

- COLD → WARM after **2 accesses within 30 seconds**.
- WARM → HOT after **3 accesses within 30 seconds**.
- History resets on each migration, so these are fresh accesses in each tier.
- Promotion is skipped if the object cannot fit in the faster tier by itself.
- GET's `tier` is the tier that served that payload; metadata reports ownership
  after promotion. `latency_ms` includes any synchronous promotion attempt.

Before a write or migration consumes bounded space, the manager demotes the LRU
object, using last-access monotonic time and key as a deterministic tie-breaker.
WARM demotion can cascade into COLD. Room-making repeats until the incoming object
fits. The destination is admitted **before** replacing the requested object. Failed
admission can move other objects, but it does not replace the requested key.

The background task runs every 2 seconds. It retries leftover copy deletion,
evaluates the policy, and enforces capacities. An object idle for 60 seconds moves
HOT → WARM; one idle for 300 seconds moves WARM → COLD. These thresholds measure time
since its last successful read or write, not time since its most recent migration.
Each object moves at most once per evaluation. Shutdown signals the worker and waits
for the current safe operation to finish.

PUT on an existing key preserves creation time and lifetime counters, replaces the
payload, refreshes its recency timestamp, clears access history, and selects a new
placement from HOT downward. A PUT placement change is a write, not a read-driven
promotion. Metadata reads do not count as object accesses.

## Configuration

All `Settings` fields accept an `ATLASTIER_` environment variable. For example:

```powershell
$env:ATLASTIER_HOT_CAPACITY_BYTES = '32768'
$env:ATLASTIER_TIERING_ENABLED = 'false'
python -m uvicorn app.api:app --workers 1
```

| Suffix after `ATLASTIER_` | Default |
|---|---|
| `HOT_CAPACITY_BYTES`, `WARM_CAPACITY_BYTES` | `1048576`, `10485760` |
| `COLD_CAPACITY_BYTES` | `none` (unlimited; a positive integer sets a limit) |
| `HOT_LATENCY_MS`, `WARM_LATENCY_MS`, `COLD_LATENCY_MS` | `1`, `15`, `80` |
| `PROMOTION_WINDOW_SECONDS` | `30` |
| `COLD_PROMOTION_ACCESSES`, `WARM_PROMOTION_ACCESSES` | `2`, `3` |
| `HOT_IDLE_SECONDS`, `WARM_IDLE_SECONDS` | `60`, `300` |
| `BACKGROUND_INTERVAL_SECONDS` | `2` |
| `TIERING_ENABLED` | `true` |
| `DATA_DIR` | `data` locally; `/data` in Docker |
| `MAX_OBJECT_BYTES` | `16777216` |

Compose declares capacity/delay values explicitly; edit its environment section to
change them. Its tiering flag uses host environment interpolation. Invalid settings
fail startup rather than silently choosing another configuration.

## Failure and concurrency semantics

Migration writes the destination atomically, waits for success, updates metadata,
and then deletes the source. A destination-write failure preserves source ownership
and payload. A source-delete failure leaves metadata pointing to the valid destination
and records the old copy for retry. After a successful migration, exactly one tier
owns and physically stores the object.

Placement counters count committed ownership changes; a source-delete failure can
increment both a placement counter and the migration-failure counter. Physical gauges
and `/tiers` include leftover copies. Backend failure can temporarily leave a tier
over capacity (especially an oversized object's transient HOT staging); subsequent
cleanup/evaluation repairs it if IO recovers. A failed PUT can have committed its
payload before cleanup failed, so retry PUT with the same value if uncertain. There
is no transaction rollback across several LRU moves.

GET returns a payload already read successfully even if its optional promotion
fails. The failure is logged and counted; explicit `TierManager.migrate` raises.
This keeps failed optimization from making readable data unavailable.

Mutations take a placement lock followed by key locks. Reads only lock their key
while loading and recording access; they release it before requesting promotion.
There are 256 lock stripes, so unrelated keys usually read concurrently, but hash
collisions serialize. Placement writes/migrations are intentionally serialized.
In-flight mutations are shielded from cancellation until IO and accounting finish.

There is **no durable metadata, write-ahead log, recovery scan, fsync guarantee,
replication, or multi-process coordination**. A crash can leave source/destination
duplicates or temporary files. HOT memory and metadata do not survive restart.
Every startup uses `data/sessions/<random-id>/{warm,cold}`. Previous disk payloads
remain in the volume for inspection but are unreachable through the new API; they
consume real disk space outside the new session's accounting. Old sessions require
manual cleanup. Restart is a reset, not recovery.

## Observability

| Metric | Type and interpretation |
|---|---|
| `requests_total{method,route,status}` | Counter for HTTP requests, including errors |
| `reads_total`, `writes_total` | Counters for read payloads and committed writes |
| `tier_hits_total{tier}` | Counter for the actual serving tier |
| `promotions_total`, `demotions_total` | Counters for committed migration ownership changes |
| `migration_failures_total` | Counter for failed migration attempts, including admission failures |
| `background_failures_total` | Counter for background cycles that raised |
| `tier_bytes{tier}`, `tier_objects{tier}` | Gauges for physical payloads/copies, refreshed after mutations |
| `request_latency_seconds{method,route}` | Histogram of HTTP handling latency |

Route labels use templates such as `/objects/{key}`, never raw object keys.
`/metrics` counts its own request after generating that scrape. Each app has an
isolated registry; counters reset with the process. `/healthz` is a basic liveness
check, not a durability or backend-integrity check.

## Tests and failure demonstrations

```bash
python -m pytest -q
python -m pytest tests/failure_demo.py -v -s
docker compose run --rm atlastier python -m pytest -q
python tests/smoke_http.py --url http://localhost:8000
```

The suite asserts payloads, ownership, capacity, metadata, metrics, concurrency,
background lifecycle, and injected failure behavior. Failure demonstrations are
also included in the default pytest run.
The standalone HTTP smoke script exercises all three tiers against a default,
dedicated service, using uniquely named objects and removing them afterward.

## Reproducible benchmarks

The embedded comparison calls the real FastAPI application with `httpx.ASGITransport`
and actual memory/filesystem backends. It avoids network transport and is explicitly
an **in-process HTTP-handler experiment**, not a network or real-hardware benchmark.

Both scenarios use the same seeded trace and start with every object in COLD.
Setup disables artificial delays while loading; measured GETs restore them. Setup
migrations and optional warmup are excluded from reported migration counts. Background
tiering is disabled in embedded mode, while read promotion and capacity demotion run.

```bash
python -m benchmarks.run --mode compare --pattern hotspot --requests 1000 --concurrency 4 --key-space 40 --hot-capacity-bytes 32768 --warm-capacity-bytes 131072 --seed 42 --output benchmark-results/hotspot.json
python -m benchmarks.run --mode compare --pattern uniform --requests 1000 --concurrency 4 --key-space 40 --hot-capacity-bytes 32768 --warm-capacity-bytes 131072 --seed 42 --output benchmark-results/uniform.json
```

Use `--hot-key-percent 20 --hot-access-percent 80` for the default hotspot distribution.
`--object-bytes`, `--warmup-requests`, `--seed`, capacity and latency arguments are
configurable. A one-key key space is supported. Percentiles use linear interpolation
and include attempted-request latencies, including errors; throughput counts successes.
An error makes the CLI exit nonzero. Reported GET tier hits count successes only.

For an actual running server:

```bash
python -m benchmarks.run --mode adaptive --url http://localhost:8000 --pattern hotspot --requests 1000 --concurrency 10 --key-space 1000
```

Remote mode seeds/overwrites `bench-*` keys, uses server-side configuration, and includes
background activity. Use a dedicated session without other clients. The service mode
must match `--mode`; an existing adaptive service cannot be switched by this CLI.
For remote cold-only mode, start a separate service with
`ATLASTIER_TIERING_ENABLED=false`, then use `--mode cold-only`. Embedded `--mode compare`
is the controlled baseline comparison and requires no running server.

Do not infer that adaptive must win. Promotion IO, serialized migrations, changing
working sets, insufficient capacity, and short traces can make it slower. Actual
executed results and verification evidence are in [docs/experiments.md](docs/experiments.md).

## Repository and reading order

```text
AtlasTier/
├── app/                 api.py, config.py
├── storage/             base.py, memory.py, disk.py, cold.py
├── tiering/             manager.py, metadata.py, policy.py
├── observability/       metrics.py
├── benchmarks/          run.py
├── tests/
│   ├── unit/            backend, policy, manager, benchmark behavior
│   ├── integration/     HTTP API and lifecycle
│   ├── failure_demo.py
│   └── smoke_http.py    running-service HTTP checks
├── docs/
│   ├── implementation-plan.md
│   ├── architecture.md
│   ├── design-decisions.md
│   ├── experiments.md
│   ├── study-guide.md
│   └── results/         reports from executed experiments
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements.lock.txt
├── pyproject.toml
├── pytest.ini
└── README.md
```

Study `storage/base.py` and `storage/memory.py` first, then `tiering/metadata.py`,
`tiering/policy.py`, and `tiering/manager.py`. Follow a PUT and GET through `app/api.py`,
then read `tests/failure_demo.py` and `benchmarks/run.py`. The
[study guide](docs/study-guide.md) connects these files to systems concepts.
