from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    """One registry per manager, allowing isolated apps and benchmark scenarios."""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("requests_total", "HTTP requests", ["method", "route", "status"],
                                registry=self.registry)
        self.reads = Counter("reads_total", "Successful object reads", registry=self.registry)
        self.writes = Counter("writes_total", "Committed object writes", registry=self.registry)
        self.hits = Counter("tier_hits_total", "Tier that served the payload", ["tier"],
                            registry=self.registry)
        self.promotions = Counter("promotions_total", "Completed promotions", registry=self.registry)
        self.demotions = Counter("demotions_total", "Completed demotions", registry=self.registry)
        self.failures = Counter("migration_failures_total", "Failed migration attempts",
                                registry=self.registry)
        self.background_failures = Counter("background_failures_total", "Failed background cycles",
                                           registry=self.registry)
        self.tier_bytes = Gauge("tier_bytes", "Physical payload bytes", ["tier"], registry=self.registry)
        self.tier_objects = Gauge("tier_objects", "Physical object copies", ["tier"], registry=self.registry)
        self.latency = Histogram("request_latency_seconds", "HTTP request duration", ["method", "route"],
                                 buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1, 2, 5),
                                 registry=self.registry)
        for tier in ("hot", "warm", "cold"):
            self.hits.labels(tier=tier)
            self.tier_bytes.labels(tier=tier).set(0)
            self.tier_objects.labels(tier=tier).set(0)

    def sync(self, tiers) -> None:
        for name, storage in tiers.items():
            self.tier_bytes.labels(tier=name.value).set(storage.bytes_used)
            self.tier_objects.labels(tier=name.value).set(storage.object_count)
