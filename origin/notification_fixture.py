"""Fixed, entirely synthetic notification fixtures (53 keys per cohort).

HTTP gateway paths are /notification/{swr|hard}/{key}. A key is primary,
related-00..19 or cooler-00..31. Optional case queries must match
notification-{cohort}-{key}; /config uses that name to replace only that key's
behavior, without resetting accounting or caches. Missing/default cases select
the path's fixed case. No arbitrary cases or additional origins are admitted.
"""

from dataclasses import dataclass


COHORTS = ("swr", "hard")
KEYS = ("primary", *(f"related-{i:02d}" for i in range(20)),
        *(f"cooler-{i:02d}" for i in range(32)))
CACHE_CONTROL = {
    "swr": "public, max-age=60, stale-while-revalidate=30, stale-if-error=60",
    "hard": "public, max-age=60",
}
WINDOW_CAPACITY = 4096
MAX_INFLIGHT = 4096
STATUSES = (0, 200, 304, 404, 405, 500, 502, 503, 504)


@dataclass(frozen=True)
class Endpoint:
    cohort: str
    key: str

    @property
    def case(self):
        return f"notification-{self.cohort}-{self.key}"

    @property
    def key_class(self):
        return self.key.split("-")[0]

    @property
    def path(self):
        return f"/notification/{self.cohort}/{self.key}"


ENDPOINTS = {endpoint.path: endpoint
             for cohort in COHORTS for key in KEYS
             for endpoint in (Endpoint(cohort, key),)}
CASES = {endpoint.case: endpoint for endpoint in ENDPOINTS.values()}


def content(endpoint, version):
    """Stable content; only an explicit version transition changes the schema."""
    return {
        "schema": {
            "type": "synthetic-notification",
            "revision": 1,
            "fields": ["title", "message", "priority"],
        },
        "title": f"{endpoint.cohort} {endpoint.key} notification {version}",
        "message": f"Invented fixture content for {endpoint.key}, revision {version}.",
        "priority": "high" if endpoint.key == "primary" else "normal",
        "padding": "n" * 768,
    }
