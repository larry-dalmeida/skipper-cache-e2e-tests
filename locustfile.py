#!/usr/bin/env python3
"""Locust users for the Skipper cache stack; started by `python3 lifecycle.py locust`.

CohortShape drives the swarm endlessly: per-cohort VU targets in 10..30 (total <= --max-vus),
re-rolled every REFRESH_SECONDS. Stop with `./teardown.sh locust`.
"""
import random
import sys
import time
from pathlib import Path

import gevent
from locust import HttpUser, LoadTestShape, events, task
from locust.exception import StopUser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalogue  # noqa: E402
import locust_workload as wl  # noqa: E402

CATALOGUE = catalogue.Catalogue(catalogue.build_entries())
GUARD = wl.SafetyGuard()
LEDGER = wl.WindowLedger()
SETTINGS = {"active": 0, "max_vus": wl.DEFAULT_MAX_VUS, "wait": (0.5, 1.5),
            "timeout": 5.0, "environment": None}
STAT_NAMES = {wl.stat_name(cohort, family)
              for cohort in wl.COHORTS for family in CATALOGUE.families()}


@events.init_command_line_parser.add_listener
def add_arguments(parser):
    group = parser.add_argument_group("cache workload")
    group.add_argument("--max-vus", type=int, default=wl.DEFAULT_MAX_VUS,
                       help="server-side ceiling on concurrently running users")
    group.add_argument("--wait-min", type=float, default=0.5)
    group.add_argument("--wait-max", type=float, default=1.5)
    group.add_argument("--request-timeout", type=float, default=5.0)


@events.init.add_listener
def configure(environment, **_):
    options = environment.parsed_options
    try:
        wl.validate_options(options.max_vus, options.wait_min, options.wait_max,
                            options.request_timeout)
    except ValueError as error:
        raise SystemExit(f"invalid cache workload options: {error}")
    SETTINGS.update(max_vus=options.max_vus, wait=(options.wait_min, options.wait_max),
                    timeout=options.request_timeout, environment=environment)


@events.test_start.add_listener
def open_window(environment, **_):
    LEDGER.begin(time.time())
    GUARD.start_window()
    environment.runner.stats.reset_all()
    if GUARD.latched is not None:
        gevent.spawn(environment.runner.stop)


@events.test_stop.add_listener
def close_window(environment, **_):
    rows = []
    for entry in environment.stats.entries.values():
        if entry.name not in STAT_NAMES:
            continue
        p95 = entry.get_response_time_percentile(0.95)
        rows.append({"name": entry.name, "requests": entry.num_requests,
                     "failures": entry.num_failures,
                     "p95_ms": None if p95 is None else round(p95, 1)})
    LEDGER.end(time.time(), rows)


@events.request.add_listener
def record_outcome(exception=None, **_):
    if GUARD.record(wl.failure_category(exception)):
        gevent.spawn(SETTINGS["environment"].runner.stop)


class CacheUser(HttpUser):
    abstract = True
    cohort = None

    def wait_time(self):
        return random.uniform(*SETTINGS["wait"])

    def on_start(self):
        if GUARD.latched is not None or SETTINGS["active"] >= SETTINGS["max_vus"]:
            raise StopUser()
        SETTINGS["active"] += 1
        self.counted = True

    def on_stop(self):
        if getattr(self, "counted", False):
            SETTINGS["active"] -= 1
            self.counted = False

    @task
    def fetch_endpoint(self):
        entry = CATALOGUE.pick(random)
        with self.client.get(entry.path,
                             name=wl.stat_name(self.cohort, entry.family),
                             headers=wl.request_headers(self.cohort),
                             timeout=SETTINGS["timeout"],
                             catch_response=True) as response:
            problem = wl.validate_response(response.status_code, response.content,
                                           self.cohort, entry)
            if problem is not None:
                response.failure(problem)


class SwrUser(CacheUser):
    cohort = "swr"


class HardUser(CacheUser):
    cohort = "hard"


COHORT_USERS = {user.cohort: user for user in (SwrUser, HardUser)}


class CohortShape(LoadTestShape):
    """Per-cohort VU pools with random targets, re-rolled every wl.REFRESH_SECONDS.

    Each cohort's class weight equals its target and the total is their sum, so Locust's
    weighted dispatch spawns exactly the chosen number of VUs per cohort.
    """
    spawn_rate = 10

    def __init__(self):
        super().__init__()
        self.targets = None
        self.next_roll = 0.0

    def tick(self):
        now = time.monotonic()
        if self.targets is None or now >= self.next_roll:
            self.targets = wl.pick_cohort_targets(random, SETTINGS["max_vus"])
            self.next_roll = now + wl.REFRESH_SECONDS
            for cohort, user in COHORT_USERS.items():
                user.weight = self.targets[cohort]
        return sum(self.targets.values()), self.spawn_rate, list(COHORT_USERS.values())
