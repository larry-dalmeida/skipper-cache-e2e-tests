#!/usr/bin/env python3
"""Pure workload helpers for the Locust driver: fixtures, validation, safety, windows.

Kept free of Locust imports so it can be unit-tested without the dependency.
"""
import json
import urllib.parse
from collections import deque

REVISION = 1
HOST_HEADER = "cache-e2e.local"
# Synthetic, non-secret token: only needs to be stable so the cache identity is stable.
AUTHORIZATION = "Bearer cache-e2e-synthetic"
ORIGIN_TOKEN_CLASS = "unknown"
# Cohort -> Accept-Language. Fixed per cohort so each cohort is a distinct cache identity.
COHORTS = {"swr": "en-US", "hard": "de-DE"}

MIN_SAMPLES = 50
MAX_FAILURE_RATIO = 0.05
HISTORY_LIMIT = 100
MAX_VUS_CEILING = 300
DEFAULT_MAX_VUS = MAX_VUS_CEILING
# Per-cohort VU target range, re-rolled every REFRESH_SECONDS.
COHORT_VU_RANGE = (20, 150)
REFRESH_SECONDS = 60
# Each refresh boosts a few random families so they carry a larger share of requests.
HOT_FAMILY_COUNT = 3
HOT_FAMILY_FACTOR = 5
MAX_REQUEST_TIMEOUT = 30.0
FAILURE_CATEGORIES = ("status", "body", "identity", "exception")


def stat_name(cohort, family):
    """Fixed statistics names (cohort x endpoint family), never per-path."""
    return f"{cohort}/{family}"


def pick_cohort_targets(rng, ceiling, vu_range=COHORT_VU_RANGE):
    """Random VU target per cohort within vu_range, with the total not above ceiling."""
    low, high = vu_range
    if ceiling < low * len(COHORTS):
        raise ValueError(f"--max-vus must be at least {low * len(COHORTS)} for per-cohort targets")
    while True:
        targets = {cohort: rng.randint(low, high) for cohort in COHORTS}
        if sum(targets.values()) <= ceiling:
            return targets


def pick_hot_families(rng, families, count=HOT_FAMILY_COUNT):
    """Random subset of families that receive the hot-route boost for one refresh period."""
    return set(rng.sample(sorted(families), min(count, len(families))))


def request_headers(cohort):
    return {
        "Host": HOST_HEADER,
        "Authorization": AUTHORIZATION,
        "Accept-Language": COHORTS[cohort],
    }


def validate_response(status, body, cohort, entry):
    """Return None when the response matches the fixture, else a failure category."""
    if status != 200:
        return "status"
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return "body"
    if not isinstance(data, dict):
        return "body"
    # mock_origin.py answers with a fixture describing the request it actually received.
    expected = urllib.parse.urlsplit(entry.path)
    if data.get("path") != expected.path or \
            urllib.parse.parse_qs(str(data.get("query", ""))) != \
            urllib.parse.parse_qs(expected.query):
        return "identity"
    if data.get("host") != HOST_HEADER:
        return "identity"
    # The origin classifies only its literal fake tokens; the synthetic token is "unknown".
    if data.get("tokenClass") != ORIGIN_TOKEN_CLASS:
        return "identity"
    if data.get("language") != COHORTS[cohort]:
        return "identity"
    return None


def failure_category(exception):
    """Normalize a Locust request exception into one of FAILURE_CATEGORIES."""
    if exception is None:
        return None
    message = str(exception)
    return message if message in FAILURE_CATEGORIES else "exception"


def validate_options(max_vus, wait_min, wait_max, request_timeout):
    if not 1 <= max_vus <= MAX_VUS_CEILING:
        raise ValueError(f"--max-vus must be 1..{MAX_VUS_CEILING}")
    if not 0 <= wait_min <= wait_max:
        raise ValueError("--wait-min must be >= 0 and <= --wait-max")
    if not 0 < request_timeout <= MAX_REQUEST_TIMEOUT:
        raise ValueError(f"--request-timeout must be > 0 and <= {MAX_REQUEST_TIMEOUT}")


class SafetyGuard:
    """Latches the run stopped on identity mismatch or excessive failure ratio.

    The latch survives Start/Stop cycles; only restarting the driver process clears it.
    """

    def __init__(self, min_samples=MIN_SAMPLES, max_ratio=MAX_FAILURE_RATIO):
        self.min_samples = min_samples
        self.max_ratio = max_ratio
        self.latched = None
        self.requests = 0
        self.failures = 0

    def start_window(self):
        self.requests = 0
        self.failures = 0

    def record(self, category):
        """Record one outcome; return True only on the call that latches the run."""
        self.requests += 1
        if category is None:
            return False
        self.failures += 1
        if category == "identity":
            return self.trip("identity mismatch")
        if self.requests >= self.min_samples and self.failures / self.requests > self.max_ratio:
            return self.trip("failure ratio")
        return False

    def trip(self, reason):
        if self.latched is not None:
            return False
        self.latched = reason
        return True


class WindowLedger:
    """Bounded per-window history plus lifetime totals; old windows are counted, not kept."""

    def __init__(self, limit=HISTORY_LIMIT):
        self.limit = limit
        self.history = deque(maxlen=limit)
        self.open = False
        self.windows_opened = 0
        self.windows_dropped = 0
        self.lifetime_requests = 0
        self.lifetime_failures = 0

    def begin(self, started_at):
        self.open = True
        self.windows_opened += 1
        self.started_at = started_at

    def end(self, ended_at, rows):
        """Close the open window. rows: dicts with name, requests, failures, p95_ms."""
        if not self.open:
            return None
        self.open = False
        requests = sum(row["requests"] for row in rows)
        failures = sum(row["failures"] for row in rows)
        window = {"started_at": self.started_at, "ended_at": ended_at,
                  "requests": requests, "failures": failures, "by_name": list(rows)}
        if len(self.history) == self.limit:
            self.windows_dropped += 1
        self.history.append(window)
        self.lifetime_requests += requests
        self.lifetime_failures += failures
        return window
