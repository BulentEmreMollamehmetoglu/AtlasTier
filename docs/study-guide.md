# Study guide: read the system by following one object

Start with `storage/base.py`, then `storage/memory.py`. The protocol makes the same
operations available in each tier; different placement, capacity, and delay affect
the cost. Next read metadata and policy, then follow `put`, `get`, `_make_room`, and
`_move_locked` in `tiering/manager.py`. Tests supply small, concrete examples.

## Storage hierarchy and hot/cold data

A hierarchy trades scarce fast space for plentiful slower space. In AtlasTier, HOT
is in memory, while WARM and COLD are separate local directories. Their delay difference
is simulated, not a real hardware claim. Hot/cold describes observed access behavior,
not a permanent property of a file: the same object can become important again.

Try tiny capacities and PUT four equally sized objects. Inspect `/tiers` and each
metadata endpoint. `_make_room` explains why a new object makes another change owners.
Capacity limits count payload bytes, not total process memory or filesystem usage.

## Temporal and spatial locality

Temporal locality means recently accessed data is likely to be accessed again.
AtlasTier's timestamp window and LRU recency use that signal. The hotspot workload
concentrates reads on a small key subset and lets you see whether it helps placement.

Spatial locality means neighboring data is likely to be accessed together: adjacent
disk blocks, chunks from one dataset shard, or nearby pages. AtlasTier has independent
string keys and no block layout or prefetching, so it does not implement spatial
locality. Adding chunk IDs or grouped prefetch would be a distinct experiment.

## Caching versus tiering

A cache commonly keeps an extra fast copy while a durable backing store remains
authoritative. AtlasTier migrates ownership and deletes the old copy after success.
HOT is therefore not backed by an always-present COLD copy. This is why process
restart can lose HOT payloads and why failure tests inspect the only valid copy.

Read `_move_locked` and `tests/failure_demo.py` together. A source and destination
can coexist while moving, but they should not both remain after successful cleanup.

## LRU and capacity management

Least recently used chooses the object whose last access was furthest in the past.
It is simple and often useful under temporal locality. Sequential scans can evict
valuable objects; noisy workloads can also produce churn. In this implementation,
PUT refreshes recency and successful GET updates it. Metadata inspection does not.

`test_capacity_cascades_and_lru` proves a read protects one HOT object while another
is demoted. `_make_room` recursively frees WARM before HOT can send a victim there.
What happens if one object is bigger than HOT? What if COLD has a finite full capacity?
The tests show skipping undersized tiers and rejecting admission without destroying
the requested object's previous value.

## Promotion and demotion

Promotion spends IO now in the hope that future reads cost less. Demotion frees scarce
space or moves idle objects down the hierarchy. The policy needs two recent COLD
reads for WARM and three fresh WARM reads for HOT. Histories reset on migration so
a single burst does not immediately jump through both stages.

Read `policy.py` first and predict an object's next tier, then inspect `manager.get`.
Policy recommends placement; the manager still checks whether it fits and makes room.
A GET response names the serving tier, while metadata can already name the promoted
owner. The difference explains otherwise surprising benchmark tier hits.

## Tail latency

The mean hides slow outliers. p95 and p99 describe the upper part of the latency
distribution; a small sample gives weak evidence about rare tails. The benchmark
uses interpolated percentiles of client-observed GET duration, including failed
attempts, and reports failures separately.

A COLD GET triggering migration pays its read cost plus a second source read,
destination write, source deletion, and placement-lock waiting. It may be slower
than a COLD-only read. Hotspot mean latency can improve while p99 worsens. Inspect
the JSON reports instead of concluding from only a throughput number.

## Why AI/data workloads might benefit

Repeated dataset samples, popular model artifacts, embedding partitions, or selected
checkpoint shards can create temporal locality. A hierarchy might put the active
working set in fast storage while retaining less-used data cheaply. The benefit
depends on working-set size, access skew, migration cost, and available fast capacity.

AtlasTier stores small strings, not tensor blocks or model files. It models a placement
decision, not GPU memory paging, real object storage, or a training pipeline. Compare
uniform and hotspot traces to explore that decision without extrapolating simulated
millisecond delays into claims about actual AI hardware.

## Failure semantics

Ask which copy is valid at every await in `_move_locked`. Before destination put
returns, source is authoritative. After metadata switches, destination is authoritative.
If source deletion fails, the cleanup set records its stale copy. Deleting source
first would risk permanent loss when destination put fails.

Atomic rename is not a durable migration transaction. A power loss can interrupt
filesystem writes; a process crash loses metadata and can leave duplicates. The
restart test demonstrates a fresh session and retained, unreachable old disk files.
A next-stage project could add a durable manifest and recovery journal, then inject
crashes between each migration step rather than just ordinary exceptions.

## Concurrency concerns

Concurrent GETs must not lose access increments. Concurrent PUTs must not corrupt
accounting, and a read must not observe a half-moved object. Key locks protect reads
and metadata; a placement lock protects admission and mutations. See the concurrent
tests, including a forced worst-case hash collision in every stripe.

Lock ordering matters: a read must release its key lock before waiting for placement,
because a writer already holding placement may need that key. Filesystem work runs
in threads, so cancelling an await cannot stop an already-started disk write. The
shielded mutation helper waits for IO/accounting before releasing locks.

## Observability and experiment discipline

Inspect `/metrics` before and after a workload. Serving-tier hits answer where reads
actually came from. Migration counters show movement cost; physical gauges reveal
duplicates following cleanup failure. Histograms include API handling overhead.
Metrics describe one active process, not a cluster or persistent history.

Change one variable at a time: access distribution, fast capacity, object size,
concurrency, or simulated latency. Use the same seed and warmup policy for comparisons.
Keep error counts, tails, and migration volume alongside mean latency. A slower
adaptive result is useful evidence about an expensive policy, not a result to hide.
