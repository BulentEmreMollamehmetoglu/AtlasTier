# Design decisions

## One owner, not a cache hierarchy

After successful migration there is one physical payload copy. Metadata names its
owner. HOT does not act as a disposable cache of an always-durable COLD copy: losing
HOT loses that payload. This makes placement and failure semantics visible, and avoids
implying durability the project does not provide.

## Explicit fresh-session restart

There is no metadata persistence. A new manager creates a fresh session directory;
an object name from the previous run returns 404 even when its WARM/COLD blob remains
in the volume. Reconstructing file sizes in a DiskTier is possible, but it cannot
recover original keys, timestamps, histories, or authoritative ownership. A proper
recovery design would need durable metadata and a migration protocol, not just a scan.

Old directories remain untouched for inspection and manual cleanup. This trades
automatic space reclamation for explicit, non-destructive behavior.

## Payload accounting

Capacity is configured in bytes and counts payloads. It excludes keys, metadata,
interpreter overhead, and filesystem blocks. SHA-256 file names prevent traversal
and platform-reserved names. Atomic file replacement keeps an existing payload
intact if the replacement fails before installation.

## Frequency with recency and bounded history

The initial policy stores only enough recent timestamps to establish either
promotion threshold. Wall-clock UTC timestamps are for clients; monotonic time
is for intervals and LRU selection. Changing the system clock cannot unexpectedly
make an idle timeout negative. Moving between tiers clears frequency history so
the next stage needs independent evidence.

The policy's target method is independent of storage IO. A future policy can implement
the protocol, including its access-history hook. Admission and LRU selection remain
manager responsibilities, rather than policy decisions scattered across API endpoints.

## Admission before commit

Moving LRU victims before accepting the incoming object keeps healthy bounded tiers
within capacity at request completion. An object larger than a tier skips it for
final placement. New oversized objects still stage through HOT to demonstrate the
specified initial placement, after their final destination has been admitted.

There are no reservations shared across processes. One placement lock makes byte
accounting and migrations understandable. Slow migration IO can cause contention;
the benchmark deliberately includes its cost.

## Commit before cleanup

Destination-write failure keeps source ownership. After destination acknowledgment,
metadata switches before source deletion. If deletion fails, the destination is
authoritative and a cleanup set tracks the duplicate. Placement counts represent
committed ownership changes, so a migration can count as both committed placement
and failed cleanup. Cleanup is retried by the background worker and before later
mutations of that key. Errors are never converted into missing data.

After a failed PUT, the new value may already be committed. Repeating the same PUT
is safe in a single session; there is no exactly-once operation ID or rollback of
earlier victim moves. Backend errors can temporarily cause duplicate copies or
capacity overage; recovery requires the backend to become usable again.

## Benchmark scope

The controlled comparison uses isolated ASGI apps, local files, equal seeded traces,
equal initial COLD placement, and restored delays during measurement. It excludes
loading and optionally excludes warmup. It disables background work to avoid timing
dependent idle decisions while studying read-driven placement.

Network mode includes real HTTP and background work but requires independently
configured services. Different workload seeds, capacities, warmup, or initial tiers
must not be presented as equivalent cases. The runner reports losses as readily as
wins and exits nonzero when measured requests fail.

## Scope intentionally left out

No authentication, encryption, durable indexing, checksums for corruption detection,
replication, cloud SDK, database, or distributed consensus. Size validation happens
after request JSON parsing. Fair scheduling, write reservations, admission by cost,
and asynchronous migration queues are potential follow-up experiments, not claims
about this implementation.
