#!/usr/bin/env python3
"""Bounded LOCAL eviction, refresh overflow, network recovery and Grafana checks."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import select
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from origin.mock_origin import make_servers
from verify import configure, origin_count
from verify_cache_observability import (
    FAMILIES, VALKEY, changes, check, command, counters, exposition, metric_key,
    monitor_commands, port, request, stop, wait,
)


ROOT = Path(__file__).resolve().parent
SHA = "372b07b4f73fd500182bf8f960fb7e2d5ee51639"
GRAFANA = "grafana/grafana-oss:11.3.0"
spec = importlib.util.spec_from_file_location("dashboard_metrics", ROOT / "verify-metrics.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


def samples(text, name, **labels):
    import re
    result = []
    for metric, raw, value in re.findall(
            r'^([a-zA-Z_:]\w*)(\{[^}]*\})? (\S+)$', text, re.MULTILINE):
        found = dict(re.findall(r'(\w+)="([^"]*)"', raw))
        if metric == name and labels.items() <= found.items():
            result.append({"labels": found, "value": float(value)})
    return result


def scalar(text, name, **labels):
    values = samples(text, name, **labels)
    check(len(values) == 1, f"required scalar absent or ambiguous: {name} {labels}: {values}")
    return values[0]["value"]


def finite_positive(result, **labels):
    check(result.get("status") == "success", f"query failed: {result}")
    selected = [s for s in result["data"]["result"]
                if labels.items() <= s["metric"].items()]
    check(bool(selected), f"required query sample missing: {labels}")
    check(all(math.isfinite(float(s["value"][1])) and float(s["value"][1]) > 0
              for s in selected), f"required samples not finite/positive: {selected}")
    return selected


def resp_command(data):
    """Return one complete RESP array of bulk strings, retaining fragmented input."""
    if not data:
        return None
    check(data[:1] == b"*", "unexpected client RESP frame")
    end = data.find(b"\r\n")
    if end < 0:
        return None
    length = int(data[1:end])
    check(0 <= length <= 1024, "invalid client RESP array length")
    offset, values = end + 2, []
    for _ in range(length):
        if offset >= len(data):
            return None
        check(data[offset:offset + 1] == b"$", "unexpected client RESP argument")
        end = data.find(b"\r\n", offset)
        if end < 0:
            return None
        size = int(data[offset + 1:end])
        check(0 <= size <= 16 * 1024 * 1024, "invalid client RESP bulk length")
        start = end + 2
        offset = start + size + 2
        if len(data) < offset:
            return None
        check(data[offset - 2:offset] == b"\r\n", "invalid client RESP terminator")
        values.append(data[start:start + size])
    return offset, values


class FaultRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, target):
        self.target = target
        self.partitioned = threading.Event()
        self.lock = threading.Lock()
        self.events = []
        super().__init__(("127.0.0.1", 0), RelayHandler)

    def record(self, values, forwarded):
        name = values[0].decode().lower()
        if name in ("get", "set", "del"):
            with self.lock:
                self.events.append({
                    "command": name, "key": values[1].decode(),
                    "forwarded": forwarded, "time": time.time(),
                })


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(self.server.target, timeout=2) as upstream:
            upstream.settimeout(2)
            pending = b""
            while True:
                readable, _, _ = select.select([self.request, upstream], [], [], 1)
                for source in readable:
                    try:
                        chunk = source.recv(65536)
                    except (ConnectionResetError, OSError):
                        return
                    if not chunk:
                        return
                    if source is upstream:
                        try:
                            self.request.sendall(chunk)
                        except OSError:
                            return
                        continue
                    pending += chunk
                    while parsed := resp_command(pending):
                        end, values = parsed
                        data, pending = pending[:end], pending[end:]
                        name = values[0].decode().lower()
                        cut = self.server.partitioned.is_set() and name in ("get", "set", "del")
                        self.server.record(values, not cut)
                        if cut:
                            return
                        upstream.sendall(data)


def json_http(url, payload=None):
    req = urllib.request.Request(
        url, data=None if payload is None else json.dumps(payload).encode(),
        headers={} if payload is None else {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"sha": args.sha, "status": "ERROR", "cases": [], "errors": [],
              "scope": "owned LOCAL resources only; no production traffic",
              "not_exercised": [
                  "24-hour wall-clock retention and production workload",
                  "exhaustive RFC directive/method/validator combinations",
                  "real retail locale taxonomy",
                  "encode and unknown-purpose StorageAPI cases: separately assigned to implementation child",
              ]}
    try:
        checkout = Path(args.checkout).resolve()
        check(command("git", "-C", str(checkout), "rev-parse", "HEAD") == args.sha,
              "exact source SHA mismatch")
        check(not command("git", "-C", str(checkout), "status", "--porcelain"),
              "candidate source must remain clean")
        binary = output / "skipper"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/skipper"],
                       cwd=checkout, timeout=180, check=True)
        prom_binary = Path(args.prometheus_binary).resolve()
        report["provenance"] = {
            "version": command(str(binary), "-version"),
            "build_info": command("go", "version", "-m", str(binary)),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "prometheus_version": command(str(prom_binary), "--version"),
            "prometheus_sha256": hashlib.sha256(prom_binary.read_bytes()).hexdigest(),
            "valkey_image": json.loads(command("docker", "image", "inspect", VALKEY))[0]["RepoDigests"],
        }
        with contextlib.ExitStack() as stack:
            servers = make_servers(http_port=0, control_port=0)
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            origin = f"http://127.0.0.1:{servers[0].server_port}"
            control = f"http://127.0.0.1:{servers[1].server_port}"

            def events(marker):
                return json_http(control + "/events?" + urllib.parse.urlencode({"case": marker}))

            def launch(name, cache_filter, memory, extra=(), route_name=None):
                http, support = port(), port()
                routes = output / f"{name}.eskip"
                routes.write_text(
                    f'{route_name or name}__cache: PathSubtree("/get") -> {cache_filter} -> "{origin}";\n'
                    'ready: Path("/ready") -> healthcheck() -> status(200) -> <shunt>;\n')
                argv = [str(binary), f"-address=127.0.0.1:{http}",
                        f"-support-listener=127.0.0.1:{support}", "-metrics-flavour=prometheus",
                        "-proxy-preserve-host", f"-cache-l1-max-memory-bytes={memory}",
                        "-swarm-valkey-dial-timeout=2s", "-swarm-valkey-conn-timeout=2s",
                        "-wait-for-healthcheck-interval=0", f"-routes-file={routes}", *extra]
                report.setdefault("commands", {})[name] = argv
                log = stack.enter_context((output / f"{name}.log").open("w"))
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                stack.callback(stop, process)
                base, metric = f"http://127.0.0.1:{http}", f"http://127.0.0.1:{support}"

                def ready():
                    check(process.poll() is None, f"{name} exited")
                    try:
                        return request(base + "/ready")["status"] == 200
                    except OSError:
                        return False
                wait(ready, f"{name} readiness failed")
                return base, metric

            eviction, eviction_metrics = launch("eviction", 'cache("60s","1s","30s")', 256 * 1400)
            queue, queue_metrics = launch("queue", 'cache("3s","1s","60s","60s")', 8 * 1024 * 1024)
            sie, sie_metrics = launch("sie", 'cache("300ms","100ms","100ms","3s")', 8 * 1024 * 1024)
            container = "remaining-valkey-" + uuid.uuid4().hex[:12]
            command("docker", "run", "--detach", "--name", container,
                    "--publish", "127.0.0.1::6379", VALKEY,
                    "valkey-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "rm", "--force", container)

            def redis(*argv):
                return command("docker", "exec", container, "valkey-cli", "--raw", *argv)

            wait(lambda: redis("ping") == "PONG", "Valkey not ready")
            redis_port = int(command("docker", "port", container, "6379/tcp").rsplit(":", 1)[1])
            relay = FaultRelay(("127.0.0.1", redis_port))
            threading.Thread(target=relay.serve_forever, daemon=True).start()
            stack.callback(relay.server_close)
            stack.callback(relay.shutdown)
            l2_flags = ["-enable-swarm", "-enable-l2-cache", "-cache-l1-ttl=0s",
                        f"-swarm-valkey-urls=127.0.0.1:{relay.server_address[1]}"]
            partition, partition_metrics = launch(
                "partition", 'cache("60s","1s","30s","60s")', 8 * 1024 * 1024, l2_flags)
            monitor_file = output / "valkey-monitor.log"
            monitor_log = stack.enter_context(monitor_file.open("w"))
            monitor = subprocess.Popen(["docker", "exec", container, "valkey-cli", "MONITOR"],
                                       stdout=monitor_log, stderr=subprocess.STDOUT)
            stack.callback(stop, monitor)
            wait(lambda: "OK" in monitor_file.read_text(), "MONITOR not ready")
            prom_port = port()
            config = output / "prometheus.yml"
            config.write_text(
                "global:\n  scrape_interval: 1s\nscrape_configs:\n"
                "  - job_name: skipper-local\n    static_configs:\n" + "".join(
                    f"      - targets: ['{url.removeprefix('http://')}']\n"
                    f"        labels: {{run_id: remaining, instance: {name}}}\n"
                    for name, url in (("eviction", eviction_metrics), ("queue", queue_metrics),
                                      ("sie", sie_metrics), ("partition", partition_metrics))))
            prom_log = stack.enter_context((output / "prometheus.log").open("w"))
            prom_argv = [str(prom_binary), f"--config.file={config}",
                         f"--storage.tsdb.path={output / 'tsdb'}",
                         f"--web.listen-address=127.0.0.1:{prom_port}"]
            report["prometheus_command"] = prom_argv
            prom_process = subprocess.Popen(prom_argv, stdout=prom_log, stderr=subprocess.STDOUT)
            stack.callback(stop, prom_process)
            prom = f"http://127.0.0.1:{prom_port}"

            def query(expr, base=prom, when=None):
                parameters = {"query": expr, "timeout": "20s"}
                if when is not None:
                    parameters["time"] = str(when)
                params = urllib.parse.urlencode(parameters)
                return json_http(base + "/api/v1/query?" + params)

            def prom_ready():
                try:
                    return len(query('up{job="skipper-local"} == 1')["data"]["result"]) == 4
                except OSError:
                    return False
            wait(prom_ready, "Prometheus sources not ready", 20)
            dashboard = json.loads((ROOT / "dashboards/cache-performance.json").read_text())
            variables = vm.dashboard_variables(dashboard)

            def panels(items):
                for panel in items:
                    yield panel
                    yield from panels(panel.get("panels", []))

            targets = {p["id"]: (p["title"], vm.expand_query(p["targets"][0]["expr"], variables))
                       for p in panels(dashboard["panels"]) if p.get("targets")}

            def panel_query(id_, **required):
                title, expr = targets[id_]
                when = time.time()
                value = query(expr, when=when)
                finite_positive(value, **required)
                entry = {"panel": id_, "title": title, "expression": expr, "direct": value,
                         "evaluation_time": when, "required_labels": required}
                report.setdefault("dashboard_queries", []).append(entry)
                return entry

            def observe(name, action):
                started = time.monotonic()
                details = action()
                report["cases"].append({"name": name, "status": "PASS",
                                        "seconds": time.monotonic() - started, **details})
                print("PASS:", name, flush=True)

            def evictions():
                marker = "eviction-" + uuid.uuid4().hex
                configure(control, marker, vary="")
                paths = ["/get/" + str(i).zfill(3) + "?case=" + marker for i in range(400)]
                for path in paths[:300]:
                    result = request(eviction + path)
                    check(result["status"] == 200 and result["cache"] == "MISS",
                          f"eviction seed not cold: {result}")
                before = scalar(exposition(eviction_metrics), "skipper_custom_total",
                                key="cache.lru_eviction")
                check(before > 0, "bounded LRU pressure did not cause actual evictions")
                time.sleep(2.2)
                for path in paths[300:]:
                    check(request(eviction + path)["status"] == 200, "eviction load failed")
                text = exposition(eviction_metrics)
                evicted = scalar(text, "skipper_custom_total", key="cache.lru_eviction")
                check(evicted > before, "evictions did not increase across scrapes")
                check(not samples(text, "skipper_custom_total", key="cache.lru_oversized"),
                      "oversized rejection is not an eviction")
                probes = [request(eviction + path, {"Cache-Control": "only-if-cached"})
                          for path in paths]
                hits = sum(r["status"] == 200 and r["cache"] == "HIT" for r in probes)
                misses = sum(r["status"] == 504 and r["cache"] == "MISS" for r in probes)
                check(hits + misses == len(paths), "unexpected cache-only pressure response")
                check(misses == evicted, f"eviction oracle: absent={misses}, counter={evicted}")
                check(origin_count(control, marker) == len(paths), "cache-only probes reached origin")
                text = exposition(eviction_metrics)
                entries = scalar(text, "skipper_cache_lru_entries", route="eviction__cache")
                used = scalar(text, "skipper_cache_lru_bytes", route="eviction__cache")
                capacity = scalar(text, "skipper_cache_lru_capacity_bytes")
                check(entries == hits and 0 < used <= capacity == 256 * 1400,
                      "actual retained entries/bytes differ from independent membership oracle")
                time.sleep(2.2)
                panel_query(31, instance="eviction", key="cache.lru_eviction")
                panel_query(60, instance="eviction")
                return {"origin_calls": len(paths), "cache_only_hits": hits,
                        "cache_only_misses": misses, "evictions": evicted,
                        "bytes": used, "capacity": capacity, "oversized_series": "absent"}

            observe("real L1 capacity eviction and finite positive dashboard rates", evictions)

            def queue_overflow():
                marker = "queue-" + uuid.uuid4().hex
                configure(control, marker, vary="")
                paths = [f"/get/{i:03d}?case={marker}" for i in range(260)]
                for path in paths:
                    check(request(queue + path)["cache"] == "MISS", "queue seed not cold")
                last_seed = time.monotonic()
                configure(control, marker, vary="", barrier=True, barrier_timeout=30)
                delay = max(0, last_seed + 3.1 - time.monotonic())
                time.sleep(delay)
                try:
                    check(request(queue + paths[0])["cache"] == "STALE", "L1 stale not served")
                    wait(lambda: events(marker)["inflight"] == 1, "worker not blocked at origin")
                    for path in paths[1:257]:
                        check(request(queue + path)["cache"] == "STALE", "queued stale not served")
                    wait(lambda: samples(exposition(queue_metrics), "skipper_custom_gauges",
                                         key="cache.reval_queue_depth") == [
                                             {"labels": {"key": "cache.reval_queue_depth"},
                                              "value": 256.0}],
                         "queue did not actually reach its 256-job capacity", 12)
                    check(request(queue + paths[257])["cache"] == "STALE", "drop did not serve stale")
                    check(scalar(exposition(queue_metrics), "skipper_custom_total",
                                 key="cache.reval_dropped") == 1, "first overflow not counted")
                    time.sleep(2.2)
                    for path in paths[258:]:
                        check(request(queue + path)["cache"] == "STALE", "drop response changed")
                    check(origin_count(control, marker) == 261,
                          "queued or dropped refreshes reached blocked origin")
                    text = exposition(queue_metrics)
                    check(scalar(text, "skipper_custom_total", key="cache.reval_dropped") == 3,
                          "expected exactly three full-queue drops")
                    time.sleep(2.2)
                    panel_query(19, instance="queue", key="cache.reval_dropped")
                    panel_query(34, instance="queue")
                    panel_query(53, family="queue", outcome="dropped")
                    saturated = text
                finally:
                    json_http(control + "/release", {"case": marker})
                wait(lambda: samples(exposition(queue_metrics), "skipper_cache_route_reval_total",
                                     route="queue__cache", outcome="success") == [
                                         {"labels": {"route": "queue__cache", "outcome": "success"},
                                          "value": 257.0}],
                     "accepted jobs did not finish successfully", 20)
                text = exposition(queue_metrics)
                for outcome, expected in (("started", 257), ("success", 257), ("dropped", 3)):
                    check(scalar(text, "skipper_cache_route_reval_total",
                                 route="queue__cache", outcome=outcome) == expected,
                          f"refresh {outcome} mismatch")
                check(origin_count(control, marker) == 517, "accepted refresh origin count mismatch")
                actual = events(marker)
                dropped_paths = {urllib.parse.urlsplit(p).path for p in paths[257:]}
                check(sum(e["path"] in dropped_paths for e in actual["events"]) == 3,
                      "a dropped key was refreshed")
                (output / "queue-saturated.prom").write_text(saturated)
                return {"priming_origin": 260, "stale_l1_responses": 260,
                        "blocked_worker": 1, "queue_capacity_observed": 256,
                        "dropped": 3, "refresh_started": 257, "refresh_success": 257,
                        "total_origin_calls": 517,
                        "barrier_budget_seconds": 30, "ttl_wait_seconds": delay}

            observe("HTTP-triggered L1 stale serving and real full-queue refresh drops", queue_overflow)

            def sie_window():
                probes = []
                for status in (500, 502, 503, 504, "disconnect"):
                    marker = "sie-" + str(status) + "-" + uuid.uuid4().hex
                    configure(control, marker, vary="")
                    url = sie + "/get?case=" + marker
                    primed = request(url)
                    check(primed["status"] == 200 and primed["cache"] == "MISS", "SIE seed failed")
                    probes.append((status, marker, url, primed["body"], time.monotonic()))
                time.sleep(max(0, probes[-1][4] + .5 - time.monotonic()))
                results = []
                for status, marker, url, body, _ in probes:
                    configure(control, marker, vary="", status=503 if status == "disconnect" else status,
                              disconnect=status == "disconnect")
                    response = request(url)
                    check(response["status"] == 200 and response["cache"] == "STALE"
                          and response["body"] == body, f"SIE {status} not retained: {response}")
                    check(origin_count(control, marker) == 2, "SIE must actually attempt origin")
                    results.append({"origin_fault": status, "response": response, "origin_calls": 2})
                text = exposition(sie_metrics)
                check(scalar(text, FAMILIES[0], result="stale", tier="l1") == 5,
                      "SIE results must be stale/l1")
                time.sleep(max(0, probes[-1][4] + 3.4 - time.monotonic()))
                for status, marker, url, body, _ in probes[:4]:
                    response = request(url)
                    check(response["status"] == status and response["cache"] != "STALE",
                          "SIE retained representation beyond configured window")
                    check(origin_count(control, marker) == 3, "expired SIE origin not reached")
                return {"within_window": results, "beyond_window_http_faults": 4,
                        "ttl_seconds": .3, "swr_seconds": .1, "sie_seconds": 3,
                        "not_exercised": "exhaustive RFC directives and post-expiry transport fallback"}

            observe("bounded L1 stale-if-error HTTP faults and transport disconnect", sie_window)

            def network():
                marker = "partition-" + uuid.uuid4().hex
                configure(control, marker, vary="")
                url = partition + "/get?case=" + marker
                check(request(url)["cache"] == "MISS", "partition prime failed")
                cases = []

                def probe(name, target, headers, expected_request, expected_operations, origins,
                          status=200, cache=None, method="GET"):
                    before = counters(exposition(partition_metrics))
                    origin_before = origin_count(control, marker)
                    offset = len(relay.events)
                    started = time.monotonic()
                    result = request(target, headers, method)
                    elapsed = time.monotonic() - started
                    after = counters(exposition(partition_metrics))
                    delta = changes(before, after)
                    request_delta = {k: v for k, v in delta.items() if k.startswith(FAMILIES[0])}
                    storage_delta = {k: v for k, v in delta.items() if k.startswith(FAMILIES[1])}
                    check(request_delta == {metric_key(FAMILIES[0], **expected_request): 1},
                          f"{name}: request classification {request_delta}")
                    expected = {metric_key(FAMILIES[1], operation=op, purpose=purpose, outcome=outcome): n
                                for op, purpose, outcome, n in expected_operations}
                    check(storage_delta == expected, f"{name}: storage counts {storage_delta}, expected {expected}")
                    check(result["status"] == status and (cache is None or result["cache"] == cache),
                          f"{name}: HTTP outcome {result}")
                    check(origin_count(control, marker) - origin_before == origins,
                          f"{name}: independent origin mismatch")
                    check(not any(k.startswith(FAMILIES[2]) for k in delta),
                          "network failure incorrectly counted as processing failure")
                    trace = relay.events[offset:]
                    check(all(not event["forwarded"] for event in trace)
                          if relay.partitioned.is_set() else all(event["forwarded"] for event in trace),
                          "fault relay transmission mode mismatch")
                    if relay.partitioned.is_set():
                        for op in ("get", "set", "del"):
                            logical = sum(n for operation, _, _, n in expected_operations
                                          if operation == {"del": "delete"}.get(op, op))
                            check(sum(e["command"] == op for e in trace) <= logical,
                                  "visible retransmission exceeded logical invocation count")
                    cases.append({"name": name, "response": result, "metric_deltas": delta,
                                  "origin_delta": origins, "seconds": elapsed, "wire_attempts": trace})

                relay.partitioned.set()
                try:
                    probe("partition cache-only failure", url, {"Cache-Control": "only-if-cached"},
                          {"result": "error", "tier": "none"},
                          [("get", "metadata", "error", 1), ("get", "response", "error", 1)],
                          0, 504, "MISS")
                    cold = url + "&new=1"
                    probe("partition origin recovery with L1 persistence fallback", cold, {},
                          {"result": "error", "tier": "none"},
                          [("get", "metadata", "error", 1), ("get", "response", "error", 2),
                           ("set", "response", "error", 1)], 1, cache="MISS")
                    probe("partition fallback serves fresh L1", cold, {},
                          {"result": "fresh", "tier": "l1"},
                          [("get", "metadata", "error", 1)], 0, cache="HIT")
                    probe("partition invalidation remains best effort", url, {},
                          {"result": "bypass", "tier": "none"},
                          [("get", "metadata", "error", 1), ("delete", "response", "error", 1),
                           ("delete", "metadata", "error", 1)], 1, method="POST")
                finally:
                    relay.partitioned.clear()
                probe("network recovered shared entry is readable", url, {"Cache-Control": "only-if-cached"},
                      {"result": "fresh", "tier": "l2"},
                      [("get", "metadata", "miss", 1), ("get", "response", "success", 1)],
                      0, cache="HIT")
                probe("network recovered persistence succeeds", cold, {"Cache-Control": "no-cache"},
                      {"result": "bypass", "tier": "none"},
                      [("get", "metadata", "miss", 1), ("set", "response", "success", 1)],
                      1, cache="MISS")
                peer, peer_metrics = launch(
                    "peer", 'cache("60s","1s","30s","60s")', 8 * 1024 * 1024,
                    l2_flags, route_name="partition")
                peer_response = request(peer + "/get?case=" + marker + "&new=1")
                check(peer_response["status"] == 200 and peer_response["cache"] == "HIT",
                      "recovered write was not available to a fresh peer")
                check(scalar(exposition(peer_metrics), FAMILIES[0], result="fresh", tier="l2") == 1,
                      "peer must serve the recovered shared representation from L2")
                check(origin_count(control, marker) == 4, "peer unexpectedly reached origin")
                text = exposition(partition_metrics)
                (output / "partition-final.prom").write_text(text)
                trace = [e for case in cases for e in case["wire_attempts"]]
                check(any(not e["forwarded"] for e in trace), "network fault never hit actual socket")
                (output / "valkey-relay.json").write_text(json.dumps(relay.events, indent=2) + "\n")
                monitor_log.flush()
                wire = monitor_commands(monitor_file.read_text())
                check(len([e for e in wire if e[0] == "set"]) == 2,
                      "partition SETs unexpectedly reached actual Valkey")
                return {"probes": cases, "peer_response": peer_response,
                        "peer_request_classification": "fresh/l2",
                        "origin_total": origin_count(control, marker),
                        "monitor_data_commands": wire,
                        "retry_semantics": "net/valkey.go sets DisableRetry=true; metrics count client methods. "
                        "Relay records visible attempts; failed methods may fail before transmission. "
                        "Connection re-handshakes are not extra GET/SET/DEL operations."}

            observe("owned Valkey data-plane partition, L1 fallback and network recovery", network)

            grafana = {"status": "NOT EXERCISED"}
            if args.grafana:
                grafana_name = "remaining-grafana-" + uuid.uuid4().hex[:12]
                image = json.loads(command("docker", "image", "inspect", GRAFANA))[0]
                report["provenance"]["grafana_image"] = image["RepoDigests"]
                command("docker", "run", "--detach", "--name", grafana_name,
                        "--memory=256m", "--cpus=0.5", "--publish", "127.0.0.1::3000",
                        "--env", "GF_AUTH_ANONYMOUS_ENABLED=true",
                        "--env", "GF_AUTH_ANONYMOUS_ORG_ROLE=Admin",
                        "--env", "GF_AUTH_DISABLE_LOGIN_FORM=true",
                        "--env", "GF_ANALYTICS_REPORTING_ENABLED=false",
                        "--env", "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
                        "--env", "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false",
                        "--env", "GF_PLUGINS_PREINSTALL_DISABLED=true", GRAFANA)
                stack.callback(command, "docker", "rm", "--force", grafana_name)
                grafana_port = command("docker", "port", grafana_name, "3000/tcp").rsplit(":", 1)[1]
                grafana_url = f"http://127.0.0.1:{grafana_port}"

                def grafana_ready():
                    try:
                        return json_http(grafana_url + "/api/health")["database"] == "ok"
                    except OSError:
                        return False
                wait(grafana_ready, "owned local Grafana readiness", 60)
                json_http(grafana_url + "/api/datasources", {
                    "name": "local-prometheus", "uid": "local-prometheus", "type": "prometheus",
                    "access": "proxy", "url": f"http://host.docker.internal:{prom_port}",
                    "isDefault": True, "jsonData": {"httpMethod": "GET"}})
                check(json_http(grafana_url + "/api/dashboards/db", {
                    "dashboard": dashboard, "overwrite": True})["status"] == "success",
                    "local dashboard import failed")
                for entry in report["dashboard_queries"]:
                    params = urllib.parse.urlencode({
                        "query": entry["expression"], "timeout": "20s",
                        "time": str(entry["evaluation_time"])})
                    proxy = json_http(grafana_url + "/api/datasources/proxy/uid/local-prometheus"
                                      "/api/v1/query?" + params)
                    finite_positive(proxy, **entry["required_labels"])
                    check(proxy["data"]["result"] == entry["direct"]["data"]["result"],
                          f"Grafana proxy/direct disagreement at panel {entry['panel']}")
                    entry["grafana_proxy"] = proxy
                grafana = {"status": "PASS", "health": json_http(grafana_url + "/api/health"),
                           "url": grafana_url + "/d/cache-performance-local?from=now-5m&to=now",
                           "imported_uid": "cache-performance-local",
                           "proxy_panel_queries": len(report["dashboard_queries"]),
                           "ui_rendering": "not yet inspected"}
                print("BROWSER_READY", grafana["url"], flush=True)
            report["grafana"] = grafana
            for name, url in (("eviction", eviction_metrics), ("queue", queue_metrics),
                              ("sie", sie_metrics), ("partition", partition_metrics)):
                (output / f"{name}-final.prom").write_text(exposition(url))
            report["status"] = "PASS"
            (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            if args.ui_hold_seconds and args.grafana:
                release = output / "browser-release"
                deadline = time.monotonic() + args.ui_hold_seconds
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(.2)
        report["resources"] = "all owned processes stopped and containers removed"
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
        report["status"] = "FAIL" if isinstance(error, AssertionError) else "ERROR"
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "errors": report["errors"],
                      "output": str(output / "result.json")}), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--sha", default=SHA)
    parser.add_argument("--output", default=".runtime/remaining372b")
    parser.add_argument("--prometheus-binary", default=str(
        ROOT / ".runtime/prometheus-native/prometheus-2.55.1.darwin-arm64/prometheus"))
    parser.add_argument("--grafana", action="store_true")
    parser.add_argument("--ui-hold-seconds", type=float, default=0)
    raise SystemExit(run(parser.parse_args()))
