# Implementation plan

1. [x] Implement configurable memory, atomic disk, and simulated cold storage, with
   metadata and a replaceable frequency/recency policy.
2. [x] Build TierManager with per-key read locks, serialized placement mutations,
   cascading LRU demotion, copy-before-delete migration, and cancellation handling.
3. [x] Add typed FastAPI endpoints, bounded metric labels, and a lifespan-managed
   background worker. Define fresh-session restart semantics.
4. [x] Add behavior tests and injected failures. Build seeded uniform/hotspot workload
   generation and a fair, isolated COLD-only comparison.
5. [x] Add Docker Compose, architecture/design/study documentation, and run tests,
   failure demonstrations, benchmarks, and Docker verification where available.

Verification evidence and remaining limitations are recorded in experiments.md.
