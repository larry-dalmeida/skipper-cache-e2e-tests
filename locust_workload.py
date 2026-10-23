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
# Cohort -> Accept-Language. Fixed per cohort so each cohort is a distinct cache identity.
COHORTS = {"swr": "en-US", "hard": "de-DE"}
KEYS_PER_COHORT = 53
# Key index ranges within a cohort: one primary, 20 related, 32 cooler.
RELATED_INDEXES = range(1, 21)
COOLER_INDEXES = range(21, 53)
PRIMARY_SHARE = 0.70
RELATED_SHARE = 0.20  # the remainder (0.10) selects cooler keys

MIN_SAMPLES = 50
MAX_FAILURE_RATIO = 0.05
HISTORY_LIMIT = 100
DEFAULT_MAX_VUS = 5
MAX_VUS_CEILING = 50
MAX_REQUEST_TIMEOUT = 30.0
FAILURE_CATEGORIES = ("status", "body", "identity", "exception")


def key_path(cohort, index):
    if cohort not in COHORTS:
        raise ValueError(f"unknown cohort: {cohort}")
    if not 0 <= index < KEYS_PER_COHORT:
        raise ValueError(f"key index out of range: {index}")
    query = urllib.parse.urlencode(
        [("cohort", cohort), ("key", f"{index:02d}"), ("rev", REVISION)])
    return "/get?" + query


def key_class(index):
    if index == 0:
        return "primary"
    if index in RELATED_INDEXES:
        return "related"
    if index in COOLER_INDEXES:
        return "cooler"
    raise ValueError(f"key index out of range: {index}")


def stat_name(cohort, klass):
    """Fixed statistics names (cohort x key class), never per-key."""
    return f"{cohort}/{klass}"


def pick_key(rng, cohort):
    """Return (key_class, index) using 70/20/10 weights within the cohort."""
    roll = rng.random()
    if roll < PRIMARY_SHARE:
        return "primary", 0
    if roll < PRIMARY_SHARE + RELATED_SHARE:
        return "related", rng.choice(list(RELATED_INDEXES))
    return "cooler", rng.choice(list(COOLER_INDEXES))


def request_headers(cohort):
    return {
        "Host": HOST_HEADER,
        "Authorization": AUTHORIZATION,
        "Accept-Language": COHORTS[cohort],
    }


def validate_response(status, body, cohort, index):
    """Return None when the response matches the fixture, else a failure category."""
    if status != 200:
        return "status"
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return "body"
    if not isinstance(data, dict):
        return "body"
    expected = urllib.parse.urlsplit(key_path(cohort, index))
    actual = urllib.parse.urlsplit(str(data.get("url", "")))
    if actual.path != expected.path or \
            urllib.parse.parse_qs(actual.query) != urllib.parse.parse_qs(expected.query):
        return "identity"
    echoed = data.get("headers") or {}
    if echoed.get("Host") != [HOST_HEADER]:
        return "identity"
    if echoed.get("Authorization") != [AUTHORIZATION]:
        return "identity"
    if echoed.get("Accept-Language") != [COHORTS[cohort]]:
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
