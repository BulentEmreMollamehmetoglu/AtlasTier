# Architecture

AtlasTier stores strings as UTF-8 bytes. FastAPI handles JSON and HTTP; TierManager
owns placement and metadata. The backends intentionally do not decide eviction or
reject over-capacity writes themselves: this lets the manager perform temporary
copy-before-delete migrations and demonstrate hierarchy-level admission.

```text
                    API request / background cycle
                                  |
                                  v
                            TierManager
                     +------------+-------------+
                     |            |             |
                 metadata      policy        metrics
                     |
             authoritative current_tier
                     |
          +----------+----------+
          |          |          |
        Memory     Disk       Cold (Disk subclass)
         HOT       WARM        COLD
```

## Read

1. Lock the key stripe and find authoritative metadata.
2. Read the owner backend and verify it returned a payload.
3. Update UTC display timestamps, monotonic recency, lifetime count, and window history.
4. Record the serving tier and release the key lock.
5. If promotion is requested, take the placement lock then the key lock, re-evaluate
   policy, make room in the destination, and migrate.
6. Return the read value with its serving tier and total manager latency.

A concurrent PUT/DELETE can happen between steps 4 and 5. Re-evaluation prevents stale
promotion decisions; if metadata was deleted/recreated, the original read does not
promote the new object. Reads can return a value observed before a concurrent write,
as expected for overlapping requests.

## Write and admission

1. Take the placement lock, then the key lock; finish pending cleanup for that key.
2. Select the first tier that can hold the complete payload, starting at HOT (or COLD
   in baseline mode).
3. Make room with cascading LRU moves. Replacement admission accounts for existing
   bytes already in the target tier.
4. Atomically write the new payload before changing metadata or deleting the old copy.
5. Commit metadata, then clean up a prior owner when overwriting across tiers.
6. New oversized adaptive objects briefly enter HOT and immediately migrate to the
   pre-admitted destination. Update gauges and return final placement.

Admissions can move existing objects even if a later operation fails. This is not a
multi-object transaction. Finite COLD capacity rejects further admission; it never
silently deletes another user's data to satisfy a request.

## Migration

```text
source get -> destination put succeeds -> metadata switches -> source delete
      |                   |                                     |
  error: stop       error: source owns                     error: destination owns
                    unchanged payload                    pending source cleanup
```

The disk put uses a temporary file and `os.replace`; no partial new file is installed
on ordinary write failure. There is no fsync or recovery log. Source-delete failure
keeps both copies until cleanup; physical tier gauges reflect this. Retry cleanup
before another mutation of the same key so a stale pending copy cannot delete its
new owner.

## Locks and cancellation

The only lock order for mutations is placement → key stripes. Recursive admission
carries the set of already-held stripes so collisions never require reacquiring a
non-reentrant asyncio lock. Reads release key locks before promotion. A fixed table
of 256 stripes bounds lock memory.

This design serializes slow placement IO but permits different-stripe reads to overlap.
It is intentionally simpler than reservations, optimistic versions, or a distributed
transaction protocol. A shielded mutation task completes IO/accounting before caller
cancellation is delivered; this matters because filesystem thread IO cannot be undone
by cancelling an asyncio await.

## Lifecycle

FastAPI lifespan constructs the manager, starts its background task, and closes it
on shutdown. The worker waits on a stop event with a configurable timeout; exceptions
are logged/counted and retried at the next interval. Close wakes the event and waits
for a running cycle to finish. Each session writes under a fresh UUID directory.

Metadata and payload capacity statistics describe only the active session. Retained
directories from older sessions consume disk space but are not recovered or counted.
One Docker volume stores both tier subdirectories. No external service is required.
