#!/usr/bin/env python3
"""Controlled LOCAL fixture origins; Python standard library only.

Run ``python acceptance/mock_origin.py --cert cert.pem --key key.pem``.
HTTP Pegasus, HTTPS Contentful and HTTP control listen on 8080, 8443 and
9091 respectively (override with --http-port/--https-port/--control-port).
Without a certificate/key pair only HTTP and control are started.

Every origin URL selects a scenario with ``?case=name`` (default: "default").
Control API:
  POST /config JSON {"case": "name", ...behavior fields...}
  POST /release JSON {"case": "name"}
  POST /reset JSON {} (also releases old barriers)
  GET /events; GET /metrics; GET /health
Configuration replaces a scenario, not patches it. Fields/defaults:
version="A" (A/B), status=200 (200/404/500/502/503/504), delay=0 (0..30 seconds),
barrier=false, barrier_timeout=5 (0.01..30 seconds), timeout=false,
disconnect=false (close immediately, without an HTTP response),
vary="Accept-Language" ("Accept-Language"/"*"/""),
cache_control="public, max-age=2, stale-while-revalidate=5, stale-if-error=30".
A barrier gates all requests until /release, including later arrivals; configuring
the case again installs a new gate. Unreleased gates return 504 after their bound.
timeout=true closes the connection without a response after delay (or 5 seconds
when delay is zero), modeling an origin that exceeds a shorter proxy timeout.
Client disconnects never suppress event accounting. Status 0 denotes no response.
Reset starts an isolated accounting generation; old requests cannot pollute it.

GET /events?case=name filters events, counts and inflight to that exact case.
Fixture bodies contain only version, path, query, host, tokenClass and language
(the Accept-Language value, making cacheable language variants distinguishable).
Only the
literal fake bearer tokens fake-alpha/fake-beta are classified; Authorization is
never stored, returned or logged. Metrics labels are finite origin/status values,
never scenario names, URLs, hosts or credentials. Use only fake local credentials.

Explicit --retail mode instead serves only the PUBLIC fictional retail matrix
from retail_workload.py, with meaningful synthetic content and representation
ETags. Unknown paths/locales/origins return 404/no-store; the optional invented
negative fixture returns 500. Finite family/locale labels describe actual origin
requests, never cache attribution. POST /origin-state accepts exactly
{"origin": "cms"|"gateway", "available": bool}; false disconnects that origin's
requests without stopping the control server or altering either cache.
Retail-only POST to a known fixture path returns its synthetic body/no-store
for unsafe-method invalidation diagnostics; it never changes fixture versions.

Explicit --notification mode (mutually exclusive with --retail) serves only
notification_fixture.py's 106 fixed gateway paths. /config accepts only their
fixed case names or default. Per-key configuration survives independent version
changes without /reset. Default cache headers are max-age=60, SWR=30/SIE=60 for
swr, and max-age=60 without stale extensions for hard.
Notification accounting is streaming: completed events occupy a 4096-entry ring,
active requests are separately bounded at 4096, counters/histograms are cumulative.
GET /notification-window?after=N and notification GET /events?after=N return
completion-sequenced events (seq > N), oldest/next cursors, gap, retained and
droppedIntentional. Start with after=0; persist next-1 and fail on gap.
Inflight events are returned separately, never evicted by ring wrap. /events
also accepts a fixed case filter; counters and perKey remain cumulative.
Sequences stay monotonic across /reset; generations and counters reset.
Notification bodies also contain revision (initially 0) and createdEpoch.
Each fixed-key /config call increments only that key's revision, even when A/B
is repeated, and assigns a strictly increasing createdEpoch. These values stay
stable across fetches and are included in the representation ETag. Successful
fixed-key /config returns {"ok":true,"case":...,"revision":...,"createdEpoch":...};
GET /events and /notification-window perKey expose the same metadata. Default
behavior configuration does not revise keys. /reset initializes a new generation
with revision 0 for all keys; do not reset during an age-proof campaign.
"""

import argparse
import hashlib
from collections import Counter, deque
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

if __package__:
    from . import notification_fixture as notification
else:
    import notification_fixture as notification


MAX_BODY = 16384
BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60)
LAST_MODIFIED = {
    "A": "Wed, 01 Jan 2025 00:00:00 GMT",
    "B": "Thu, 02 Jan 2025 00:00:00 GMT",
}


class ControlError(ValueError):
    """A safe, fixed diagnostic that never includes submitted values."""


@dataclass(frozen=True)
class Behavior:
    version: str = "A"
    status: int = 200
    delay: float = 0
    barrier: bool = False
    barrier_timeout: float = 5
    timeout: bool = False
    disconnect: bool = False
    vary: str = "Accept-Language"
    cache_control: str = (
        "public, max-age=2, stale-while-revalidate=5, stale-if-error=30"
    )


def validate_config(payload):
    if not isinstance(payload, dict):
        raise ControlError("JSON body must be an object")
    unknown = set(payload) - {"case", *Behavior.__dataclass_fields__}
    if unknown:
        raise ControlError("unknown configuration fields")
    case = validate_case(payload)
    fields = {key: value for key, value in payload.items() if key != "case"}
    behavior = Behavior(**fields)
    if behavior.version not in ("A", "B"):
        raise ControlError("version must be A or B")
    if type(behavior.status) is not int or behavior.status not in (200, 404, 500, 502, 503, 504):
        raise ControlError("status must be 200, 404, 500, 502, 503 or 504")
    for name, lower in (("delay", 0), ("barrier_timeout", 0.01)):
        value = getattr(behavior, name)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not lower <= value <= 30):
            raise ControlError(f"{name} must be a finite number from {lower} to 30")
    if any(type(getattr(behavior, name)) is not bool
           for name in ("barrier", "timeout", "disconnect")):
        raise ControlError("barrier, timeout and disconnect must be booleans")
    if behavior.vary not in ("Accept-Language", "*", ""):
        raise ControlError("vary must be Accept-Language, * or empty")
    if (not isinstance(behavior.cache_control, str)
            or len(behavior.cache_control) > 512
            or any(ord(char) < 32 or ord(char) > 126
                   for char in behavior.cache_control)):
        raise ControlError("cache_control must be at most 512 printable ASCII characters")
    return case, behavior


def validate_case(payload):
    case = payload.get("case")
    if not isinstance(case, str) or not case or len(case) > 128:
        raise ControlError("case must be a nonempty string of at most 128 characters")
    return case


class State:
    def __init__(self, retail=False):
        self.notification = False
        self.lock = threading.RLock()
        self.generation = 0
        self.configs = {}
        self.events = []
        self.retail = retail
        self.origins = ("cms", "gateway") if retail else ("pegasus", "contentful")
        self.available = {origin: True for origin in self.origins}
        self.availability_changed = {origin: time.time() for origin in self.origins}

    def reset(self):
        with self.lock:
            for _, gate in self.configs.values():
                gate.set()
            self.generation += 1
            self.configs = {}
            self.events = []

    def configure(self, case, behavior):
        with self.lock:
            if case in self.configs:
                self.configs[case][1].set()
            gate = threading.Event()
            if not behavior.barrier:
                gate.set()
            self.configs[case] = (behavior, gate)

    def release(self, case):
        with self.lock:
            if case not in self.configs:
                raise ControlError("case is not configured")
            self.configs[case][1].set()

    def begin(self, origin, path, headers):
        parsed = urlsplit(path)
        case = parse_qs(parsed.query, keep_blank_values=True).get("case", ["default"])[0]
        authorization = headers.get("Authorization")
        token_class = {
            "Bearer fake-alpha": "alpha", "Bearer fake-beta": "beta"
        }.get(authorization, "unknown" if authorization else "none")
        with self.lock:
            if case not in self.configs:
                gate = threading.Event()
                gate.set()
                self.configs[case] = (Behavior(), gate)
            behavior, gate = self.configs[case]
            event = {
                "id": len(self.events) + 1, "generation": self.generation,
                "origin": origin, "case": case, "path": parsed.path,
                "query": parsed.query, "host": headers.get("Host", ""),
                "language": headers.get("Accept-Language", ""),
                "ifNoneMatch": headers.get("If-None-Match", ""),
                "traceparent": headers.get("traceparent", ""),
                "tokenClass": token_class, "status": None, "latency": None,
                "startedAt": time.time(), "outcome": "inflight",
            }
            self.events.append(event)
            return behavior, gate, event

    def finish(self, event, started, status, outcome):
        with self.lock:
            event.update(status=status, latency=time.monotonic() - started,
                         outcome=outcome)

    def snapshot(self, case=None):
        with self.lock:
            events = [dict(event) for event in self.events
                      if case is None or event["case"] == case]
            return {
                "generation": self.generation, "events": events,
                "counts": {
                    "total": len(events),
                    "byStatus": dict(Counter(
                        str(event["status"]) for event in events
                        if event["status"] is not None)),
                },
                "inflight": sum(event["status"] is None for event in events),
            }

    def metrics(self):
        snapshot = self.snapshot()
        lines = [
            "# HELP mock_origin_requests_total Completed actual origin requests.",
            "# TYPE mock_origin_requests_total counter",
            "# HELP mock_origin_request_duration_seconds Actual origin request latency.",
            "# TYPE mock_origin_request_duration_seconds histogram",
            "# HELP mock_origin_inflight Current actual origin requests.",
            "# TYPE mock_origin_inflight gauge",
        ]
        for origin in self.origins:
            events = [e for e in snapshot["events"] if e["origin"] == origin]
            inflight = sum(e["status"] is None for e in events)
            lines.append(f'mock_origin_inflight{{origin="{origin}"}} {inflight}')
            for status in (0, 200, 304, 404, 405, 500, 502, 503, 504):
                durations = [e["latency"] for e in events if e["status"] == status]
                labels = f'origin="{origin}",status="{status}"'
                lines.append(f"mock_origin_requests_total{{{labels}}} {len(durations)}")
                for bucket in BUCKETS:
                    count = sum(duration <= bucket for duration in durations)
                    lines.append(
                        f'mock_origin_request_duration_seconds_bucket'
                        f'{{{labels},le="{bucket}"}} {count}')
                lines.extend([
                    f'mock_origin_request_duration_seconds_bucket'
                    f'{{{labels},le="+Inf"}} {len(durations)}',
                    f"mock_origin_request_duration_seconds_sum{{{labels}}} {sum(durations)}",
                    f"mock_origin_request_duration_seconds_count{{{labels}}} {len(durations)}",
                ])
        if self.retail:
            lines.extend(["# HELP mock_origin_available Controlled listener availability, not cache health.",
                          "# TYPE mock_origin_available gauge",
                          "# HELP mock_origin_state_changed_seconds Actual origin controller change epoch.",
                          "# TYPE mock_origin_state_changed_seconds gauge"])
            with self.lock:
                for origin in self.origins:
                    labels = f'origin="{origin}"'
                    lines.append(f'mock_origin_available{{{labels}}} {int(self.available[origin])}')
                    lines.append(f'mock_origin_state_changed_seconds{{{labels}}} '
                                 f'{self.availability_changed[origin]}')
            counts = Counter((e["family"], e["locale"], e["status"])
                             for e in snapshot["events"] if "family" in e
                             and e["status"] is not None)
            lines.extend(["# HELP mock_retail_requests_total Actual synthetic origin completions.",
                          "# TYPE mock_retail_requests_total counter"])
            for (family, locale, status), count in sorted(counts.items()):
                lines.append(f'mock_retail_requests_total{{family="{family}",'
                             f'locale="{locale}",status="{status}"}} {count}')
        return "\n".join(lines) + "\n"


class NotificationState(State):
    """Bounded streaming accounting; finite-mode State intentionally unchanged."""

    def __init__(self):
        super().__init__()
        self.notification = True
        self.origins = ("gateway", "notification-secondary")
        self.slots = threading.BoundedSemaphore(notification.MAX_INFLIGHT)
        self.sequence = 0
        self.request_sequence = 0
        self.reset()

    def reset(self):
        with self.lock:
            super().reset()
            self.events = deque(maxlen=notification.WINDOW_CAPACITY)
            self.active = {}
            self.completed = Counter()
            self.histograms = {}
            self.started_counts = Counter()
            self.dropped = 0
            created_epoch = time.time()
            self.per_key = {
                case: {"cohort": endpoint.cohort, "key": endpoint.key,
                       "fetches": 0, "completed": 0, "inflight": 0,
                       "concurrentPeak": 0, "lastFetch": None,
                       "lastVersion": None, "lastCompleted": None,
                       "revision": 0, "createdEpoch": created_epoch}
                for case, endpoint in notification.CASES.items()
            }

    def configure(self, case, behavior):
        if case not in notification.CASES and case != "default":
            raise ControlError("notification case must be a fixed fixture case or default")
        with self.lock:
            super().configure(case, behavior)
            if case in self.per_key:
                key = self.per_key[case]
                key["revision"] += 1
                key["createdEpoch"] = max(
                    time.time(), math.nextafter(key["createdEpoch"], math.inf))
                return {"ok": True, "case": case, "revision": key["revision"],
                        "createdEpoch": key["createdEpoch"]}

    def begin(self, origin, path, headers):
        parsed = urlsplit(path)
        selected = parse_qs(parsed.query, keep_blank_values=True).get("case", ["default"])
        endpoint = notification.ENDPOINTS.get(parsed.path) if origin == "gateway" else None
        if (endpoint is not None and
                (len(selected) != 1 or selected[0] not in ("default", endpoint.case))):
            endpoint = None
        case = endpoint.case if endpoint else "unknown"
        authorization = headers.get("Authorization")
        token_class = {"Bearer fake-alpha": "alpha", "Bearer fake-beta": "beta"}.get(
            authorization, "unknown" if authorization else "none")
        # Backpressure bounds active storage without evicting unfinished requests.
        self.slots.acquire()
        with self.lock:
            behavior, gate = self.configs.get(case, self.configs.get("default", (None, None)))
            if behavior is None:
                behavior = Behavior(cache_control=notification.CACHE_CONTROL[
                    endpoint.cohort if endpoint else "hard"])
                gate = threading.Event()
                gate.set()
            self.request_sequence += 1
            event = {
                "id": self.request_sequence, "generation": self.generation,
                "origin": origin, "case": case, "path": parsed.path[:1024],
                "query": parsed.query[:1024], "host": headers.get("Host", "")[:1024],
                "language": headers.get("Accept-Language", "")[:1024],
                "ifNoneMatch": headers.get("If-None-Match", "")[:1024],
                "traceparent": headers.get("traceparent", "")[:1024],
                "tokenClass": token_class, "status": None, "latency": None,
                "startedAt": time.time(), "outcome": "inflight",
                "cohort": endpoint.cohort if endpoint else "unknown",
                "keyClass": endpoint.key_class if endpoint else "unknown",
                "version": behavior.version,
            }
            self.active[event["id"]] = event
            self.started_counts[case] += 1
            if endpoint:
                key = self.per_key[case]
                key["fetches"] += 1
                key["inflight"] += 1
                key["concurrentPeak"] = max(key["concurrentPeak"], key["inflight"])
                key["lastFetch"] = event["startedAt"]
                key["lastVersion"] = behavior.version
                event.update(revision=key["revision"], createdEpoch=key["createdEpoch"])
            return behavior, gate, event

    def finish(self, event, started, status, outcome):
        try:
            with self.lock:
                super().finish(event, started, status, outcome)
                if event["generation"] != self.generation:
                    return
                self.active.pop(event["id"])
                self.sequence += 1
                event["seq"] = self.sequence
                event["completedAt"] = time.time()
                if len(self.events) == self.events.maxlen:
                    self.dropped += 1
                self.events.append(event)
                dimensions = (event["origin"], status, event["cohort"], event["keyClass"])
                self.completed[(event["case"], str(status))] += 1
                histogram = self.histograms.setdefault(
                    dimensions, {"count": 0, "sum": 0, "buckets": [0] * len(BUCKETS)})
                histogram["count"] += 1
                histogram["sum"] += event["latency"]
                for index, bucket in enumerate(BUCKETS):
                    histogram["buckets"][index] += event["latency"] <= bucket
                if event["case"] in self.per_key:
                    key = self.per_key[event["case"]]
                    key["completed"] += 1
                    key["inflight"] -= 1
                    key["lastCompleted"] = event["completedAt"]
        finally:
            self.slots.release()

    def snapshot(self, case=None, after=0):
        if case is not None and case not in notification.CASES and case != "default":
            raise ControlError("notification case must be a fixed fixture case or default")
        with self.lock:
            oldest = self.events[0]["seq"] if self.events else self.sequence + 1
            events = [dict(e) for e in self.events
                      if e["seq"] > after and (case is None or e["case"] == case)]
            active = [dict(e) for e in self.active.values()
                      if case is None or e["case"] == case]
            counts = Counter()
            for (name, status), count in self.completed.items():
                if case is None or name == case:
                    counts[status] += count
            return {
                "generation": self.generation, "events": events, "inflightEvents": active,
                "counts": {"total": (sum(self.started_counts.values()) if case is None
                                     else self.started_counts[case]), "byStatus": dict(counts)},
                "inflight": len(active), "oldest": oldest, "next": self.sequence + 1,
                "gap": after < oldest - 1, "retained": len(self.events),
                "droppedIntentional": self.dropped,
                "windowCapacity": self.events.maxlen,
                "perKey": {name: dict(value) for name, value in self.per_key.items()
                           if case is None or name == case},
            }

    def metrics(self):
        with self.lock:
            lines = [
                "# TYPE mock_origin_requests_total counter",
                "# TYPE mock_origin_request_duration_seconds histogram",
                "# TYPE mock_origin_inflight gauge",
                "# TYPE mock_notification_requests_total counter",
                "# TYPE mock_notification_inflight gauge",
                "# TYPE mock_notification_request_duration_seconds histogram",
            ]
            for origin in self.origins:
                lines.append(f'mock_origin_inflight{{origin="{origin}"}} '
                             f'{sum(e["origin"] == origin for e in self.active.values())}')
                for status in notification.STATUSES:
                    matching = [h for (o, s, _, _), h in self.histograms.items()
                                if (o, s) == (origin, status)]
                    labels = f'origin="{origin}",status="{status}"'
                    count = sum(h["count"] for h in matching)
                    lines.append(f"mock_origin_requests_total{{{labels}}} {count}")
                    for index, bucket in enumerate(BUCKETS):
                        value = sum(h["buckets"][index] for h in matching)
                        lines.append(f'mock_origin_request_duration_seconds_bucket'
                                     f'{{{labels},le="{bucket}"}} {value}')
                    lines.extend([
                        f'mock_origin_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}',
                        f'mock_origin_request_duration_seconds_count{{{labels}}} {count}',
                        f'mock_origin_request_duration_seconds_sum{{{labels}}} '
                        f'{sum(h["sum"] for h in matching)}',
                    ])
            for (origin, status, cohort, key_class), histogram in sorted(self.histograms.items()):
                labels = (f'origin="{origin}",status="{status}",'
                          f'cohort="{cohort}",keyclass="{key_class}"')
                lines.append(f'mock_notification_requests_total{{{labels}}} {histogram["count"]}')
                for bucket, count in zip(BUCKETS, histogram["buckets"]):
                    lines.append(f'mock_notification_request_duration_seconds_bucket'
                                 f'{{{labels},le="{bucket}"}} {count}')
                lines.extend([
                    f'mock_notification_request_duration_seconds_bucket'
                    f'{{{labels},le="+Inf"}} {histogram["count"]}',
                    f'mock_notification_request_duration_seconds_count'
                    f'{{{labels}}} {histogram["count"]}',
                    f'mock_notification_request_duration_seconds_sum'
                    f'{{{labels}}} {histogram["sum"]}',
                ])
            inflight = Counter((e["origin"], e["cohort"], e["keyClass"])
                               for e in self.active.values())
            for origin in self.origins:
                for cohort in (*notification.COHORTS, "unknown"):
                    for key_class in ("primary", "related", "cooler", "unknown"):
                        labels = (f'origin="{origin}",cohort="{cohort}",'
                                  f'keyclass="{key_class}"')
                        lines.append(f'mock_notification_inflight{{{labels}}} '
                                     f'{inflight[(origin, cohort, key_class)]}')
            return "\n".join(lines) + "\n"


def not_modified(headers, version, etag=None):
    etag = etag or f'"{version}"'
    if headers.get("If-None-Match") is not None:
        return any(value.strip().removeprefix("W/") in (etag, "*")
                   for value in headers["If-None-Match"].split(","))
    if headers.get("If-Modified-Since"):
        try:
            since = parsedate_to_datetime(headers["If-Modified-Since"])
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            return since >= parsedate_to_datetime(LAST_MODIFIED[version])
        except (TypeError, ValueError, OverflowError):
            pass
    return False


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each connection, preventing idle clients holding worker threads.
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, status, body, content_type="application/json", headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != 304:
            self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.server.origin == "control":
            path = urlsplit(self.path).path
            if path == "/events" or (
                    path == "/notification-window" and self.server.state.notification):
                case = parse_qs(urlsplit(self.path).query,
                                keep_blank_values=True).get("case", [None])[0]
                try:
                    if self.server.state.notification:
                        after = int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0])
                        if after < 0:
                            raise ValueError()
                        snapshot = self.server.state.snapshot(case, after)
                    else:
                        snapshot = self.server.state.snapshot(case)
                    self.reply(200, json.dumps(snapshot))
                except (ControlError, ValueError):
                    self.reply(400, '{"error":"invalid event cursor or fixed case"}')
            elif path == "/metrics":
                self.reply(200, self.server.state.metrics(),
                           "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/health":
                self.reply(200, '{"ok":true}')
            else:
                self.reply(404, '{"error":"unknown control endpoint"}')
            return
        self.origin_request()

    def do_POST(self):
        if self.server.origin != "control":
            self.origin_request()
            return
        if self.command != "POST":
            self.reply(405, '{"error":"control supports GET, HEAD and POST only"}')
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ControlError("Transfer-Encoding is not supported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise ControlError("body exceeds 16384 bytes")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ControlError("JSON body must be an object")
            path = urlsplit(self.path).path
            if path == "/config":
                case, behavior = validate_config(payload)
                if self.server.state.notification and "cache_control" not in payload:
                    endpoint = notification.CASES.get(case)
                    behavior = Behavior(**{
                        **{field: getattr(behavior, field) for field in Behavior.__dataclass_fields__},
                        "cache_control": notification.CACHE_CONTROL[
                            endpoint.cohort if endpoint else "hard"],
                    })
                configured = self.server.state.configure(case, behavior)
                if configured is not None:
                    self.reply(200, json.dumps(configured))
                    return
            elif path == "/release":
                if set(payload) != {"case"}:
                    raise ControlError("release requires only case")
                self.server.state.release(validate_case(payload))
            elif path == "/reset":
                if payload:
                    raise ControlError("reset requires an empty object")
                self.server.state.reset()
            elif path == "/origin-state" and self.server.state.retail:
                if (set(payload) != {"origin", "available"}
                        or payload["origin"] not in ("cms", "gateway")
                        or type(payload["available"]) is not bool):
                    raise ControlError("origin-state requires cms/gateway and boolean available")
                with self.server.state.lock:
                    self.server.state.available[payload["origin"]] = payload["available"]
                    self.server.state.availability_changed[payload["origin"]] = time.time()
                self.reply(200, json.dumps(payload))
                return
            else:
                self.reply(404, '{"error":"unknown control endpoint"}')
                return
            self.reply(200, '{"ok":true}')
        except ControlError as error:
            self.reply(400, json.dumps({"error": str(error)}))
        except (ValueError, TypeError, UnicodeError):
            # Do not reflect invalid payloads: they might contain real credentials.
            self.reply(400, '{"error":"invalid control request; see module API contract"}')

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_TRACE = do_POST
    do_CONNECT = do_POST

    def origin_request(self):
        started = time.monotonic()
        behavior, gate, event = self.server.state.begin(
            self.server.origin, self.path, self.headers)
        status, outcome = 0, "disconnected"
        try:
            if self.command not in ("GET", "HEAD", "POST"):
                status, outcome = 405, "unsupported_method"
                self.reply(status, '{"error":"origin supports GET, HEAD and POST only"}',
                           headers={"Cache-Control": "no-store"})
                return
            endpoint = None
            notification_endpoint = None
            if self.server.state.notification:
                notification_endpoint = notification.CASES.get(event["case"])
                if notification_endpoint is None:
                    status, outcome = 404, "unknown_endpoint"
                    self.reply(status, '{"error":"unknown synthetic endpoint"}',
                               headers={"Cache-Control": "no-store"})
                    return
            if self.server.state.retail:
                if __package__:
                    from .retail_workload import ORIGIN_ENDPOINTS, content
                else:
                    from retail_workload import ORIGIN_ENDPOINTS, content
                endpoint = ORIGIN_ENDPOINTS.get((self.server.origin, event["path"]))
                if endpoint is None:
                    status, outcome = 404, "unknown_endpoint"
                    self.reply(status, '{"error":"unknown synthetic endpoint"}',
                               headers={"Cache-Control": "no-store"})
                    return
                with self.server.state.lock:
                    event.update(family=endpoint.family, locale=endpoint.locale)
                    available = self.server.state.available[self.server.origin]
                if not available:
                    outcome = "origin_disconnect"
                    self.close_connection = True
                    return
            if behavior.disconnect:
                outcome = "disconnect"
                self.close_connection = True
                return
            if not gate.wait(behavior.barrier_timeout):
                status, outcome = 504, "barrier_timeout"
                self.reply(status, '{"error":"barrier timed out"}',
                           headers={"Cache-Control": "no-store"})
                return
            time.sleep(behavior.delay or (5 if behavior.timeout else 0))
            if behavior.timeout:
                outcome = "timeout"
                self.close_connection = True
                return
            status = behavior.status
            if endpoint and status == 200:
                status = endpoint.status
            fixture = {key: event[key] for key in ("path", "query", "host", "tokenClass")}
            fixture["version"] = behavior.version
            fixture["language"] = self.headers.get("Accept-Language", "")
            if endpoint:
                fixture.update(family=endpoint.family, locale=endpoint.locale,
                               content=content(endpoint, behavior.version))
            if notification_endpoint:
                fixture.update(
                    key=notification_endpoint.key, cohort=notification_endpoint.cohort,
                    synthetic=True, revision=event["revision"],
                    createdEpoch=event["createdEpoch"],
                    content=notification.content(notification_endpoint, behavior.version))
            etag = (f'"{hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()}"'
                    if endpoint or notification_endpoint else f'"{behavior.version}"')
            if (self.command in ("GET", "HEAD") and status == 200
                    and not_modified(self.headers, behavior.version, etag)):
                status = 304
            response_headers = {
                "ETag": etag,
                "Last-Modified": LAST_MODIFIED[behavior.version],
                "Cache-Control": "no-store" if self.command == "POST" else behavior.cache_control,
            }
            if behavior.vary:
                response_headers["Vary"] = behavior.vary
            response_body = "" if status == 304 else json.dumps(fixture)
            with self.server.state.lock:
                event.update(responseBodyBytes=len(response_body.encode()), etag=etag)
            self.reply(status, response_body,
                       headers=response_headers)
            outcome = "response"
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            outcome = "disconnected"
        finally:
            self.server.state.finish(event, started, status, outcome)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Finite parallel acceptance bursts must not inherit TCPServer's queue of five.
    request_queue_size = 128

    def __init__(self, address, state, origin):
        self.state, self.origin = state, origin
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        # Default tracebacks can print request-derived exception details.
        pass


def make_servers(host="127.0.0.1", http_port=8080, https_port=8443,
                 control_port=9091, cert=None, key=None, retail=False,
                 notification=False):
    if bool(cert) != bool(key):
        raise ValueError("cert and key must be supplied together")
    if retail and notification:
        raise ValueError("retail and notification modes are mutually exclusive")
    state, servers = NotificationState() if notification else State(retail), []
    try:
        servers.append(Server((host, http_port), state,
                              "gateway" if retail or notification else "pegasus"))
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server = Server((host, https_port), state,
                            "notification-secondary" if notification else (
                                "cms" if retail else "contentful"))
            servers.append(server)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        servers.append(Server((host, control_port), state, "control"))
        return servers
    except BaseException:
        for server in servers:
            server.server_close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--control-port", type=int, default=9091)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--retail", action="store_true",
                        help="Enable only PUBLIC fictional retail endpoint contracts")
    modes.add_argument("--notification", action="store_true",
                       help="Enable fixed synthetic notification paths and bounded streaming accounting")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be supplied together")
    servers = make_servers(**vars(args))
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in servers]
    for thread in threads:
        thread.start()
    print(json.dumps({"listeners": {
        server.origin: server.server_address[1] for server in servers
    }}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
