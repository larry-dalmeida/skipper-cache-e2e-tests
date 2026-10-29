#!/usr/bin/env python3
"""Compare request/L2 metrics with real HTTP, origin and Valkey activity."""
import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from origin.mock_origin import make_servers
from verify import configure, origin_count


ROOT = Path(__file__).resolve().parent
VALKEY = ("valkey/valkey:8.1.1-alpine@sha256:"
          "6a57d58c0a37cf7acc3045ac0fd6dd91be339774106ab4d9ca088013a096a99f")
FAMILIES = ("skipper_cache_request_total", "skipper_cache_l2_operations_total",
            "skipper_cache_l2_processing_errors_total")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def command(*argv, cwd=None):
    return subprocess.check_output(argv, cwd=cwd, text=True, timeout=180).strip()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait(predicate, message, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def exposition(url):
    with urllib.request.urlopen(url + "/metrics", timeout=15) as response:
        return response.read().decode()


def counters(text):
    result = {}
    for metric, labels, value in re.findall(
            r'^(skipper_cache_(?:request_total|l2_operations_total|l2_processing_errors_total))'
            r'\{([^}]*)\} (\S+)$', text, re.MULTILINE):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', labels))
        key = metric + json.dumps(labels, sort_keys=True, separators=(",", ":"))
        result[key] = float(value)
    return result


def changes(before, after):
    return {key: after.get(key, 0) - before.get(key, 0)
            for key in before.keys() | after.keys()
            if after.get(key, 0) != before.get(key, 0)}


def metric_key(family, **labels):
    return family + json.dumps(labels, sort_keys=True, separators=(",", ":"))


def request(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers={"Host": "observability.test", **(headers or {})},
                                 method=method)
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode()
        try:
            body = json.loads(body) if body else None
        except ValueError:
            pass
        return {"status": response.status, "cache": response.headers.get("X-Cache-Status"),
                "body": body}


def monitor_commands(text):
    commands = []
    for line in text.splitlines():
        quoted = re.findall(r'"((?:\\.|[^"\\])*)"', line)
        if len(quoted) < 2:
            continue
        name = quoted[0].lower()
        if name in ("get", "set", "setex", "psetex", "del"):
            commands.append((name, json.loads('"' + quoted[1] + '"')))
    return commands


def wire_operations(commands, existing):
    """Success/miss oracle from real command order and initial key presence."""
    counts = {}
    existing = set(existing)
    for name, key in commands:
        operation = "get" if name == "get" else "delete" if name == "del" else "set"
        purpose = "metadata" if key.startswith("vary:") else "response"
        outcome = "miss" if operation == "get" and key not in existing else "success"
        counter = metric_key(FAMILIES[1], operation=operation, purpose=purpose, outcome=outcome)
        counts[counter] = counts.get(counter, 0) + 1
        if operation == "set":
            existing.add(key)
        elif operation == "delete":
            existing.discard(key)
    return counts


def command_statistics(info):
    result = {}
    for name, fields in re.findall(r"^cmdstat_(get|set|del):([^\r\n]+)", info, re.MULTILINE):
        counts = dict(item.split("=", 1) for item in fields.split(","))
        result[{"get": "get", "set": "set", "del": "delete"}[name]] = {
            "calls": int(counts["calls"]),
            "rejected": int(counts.get("rejected_calls", 0)),
        }
    return result


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"sha": args.sha, "mode": args.mode, "status": "ERROR", "cases": [],
              "errors": [], "expected_absent_families": (
                  [FAMILIES[1], FAMILIES[2]] if args.mode == "request-source" else
                  [FAMILIES[0]] if args.mode == "storage-source" else [])}
    request_enabled = args.mode != "storage-source"
    storage_enabled = args.mode != "request-source"
    try:
        checkout = Path(args.checkout).resolve()
        check(command("git", "-C", str(checkout), "rev-parse", "HEAD") == args.sha,
              "exact source SHA mismatch")
        check(not command("git", "-C", str(checkout), "status", "--porcelain"),
              "source checkout is not clean")
        binary = output / "skipper"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/skipper"],
                       cwd=checkout, check=True, timeout=180)
        report["provenance"] = {
            "version": command(str(binary), "-version"),
            "build_info": command("go", "version", "-m", str(binary)),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "valkey_image": json.loads(command("docker", "image", "inspect", VALKEY))[0]["RepoDigests"],
        }
        with contextlib.ExitStack() as stack:
            container = "metrics-439x-" + uuid.uuid4().hex[:12]
            command("docker", "run", "--detach", "--name", container,
                    "--publish", "127.0.0.1::6379", VALKEY,
                    "valkey-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "rm", "--force", container)

            def redis(*argv):
                return command("docker", "exec", container, "valkey-cli", "--raw", *argv)

            wait(lambda: redis("ping") == "PONG", "Valkey not ready")
            redis_port = command("docker", "port", container, "6379/tcp").rsplit(":", 1)[1]
            monitor_file = output / "valkey-monitor.log"
            monitor_log = stack.enter_context(monitor_file.open("w"))
            monitor = subprocess.Popen(
                ["docker", "exec", container, "valkey-cli", "MONITOR"],
                stdout=monitor_log, stderr=subprocess.STDOUT)
            stack.callback(stop, monitor)
            wait(lambda: "OK" in monitor_file.read_text(), "Valkey MONITOR not ready")
            servers = make_servers(http_port=0, control_port=0)
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            origin = f"http://127.0.0.1:{servers[0].server_port}"
            control = f"http://127.0.0.1:{servers[1].server_port}"
            route_file = output / "routes.eskip"
            route_file.write_text(
                f'normal: Path("/get") -> cache("30s","1s","10s","30s") -> "{origin}";\n'
                f'short: Path("/short") -> cache("1s","1s","10s","30s") -> "{origin}";\n'
                f'rfc: Path("/rfc") -> cache() -> "{origin}";\n'
                'ready: Path("/ready") -> status(200) -> <shunt>;\n')
            bases, metrics = {}, {}
            for name, l1 in (("a", "30s"), ("b", "0s")):
                http_port, metrics_port = port(), port()
                bases[name], metrics[name] = (f"http://127.0.0.1:{http_port}",
                                             f"http://127.0.0.1:{metrics_port}")
                argv = [str(binary), f"-address=127.0.0.1:{http_port}",
                        f"-support-listener=127.0.0.1:{metrics_port}", "-metrics-flavour=prometheus",
                        "-enable-swarm", "-enable-l2-cache", "-proxy-preserve-host",
                        f"-swarm-valkey-urls=127.0.0.1:{redis_port}", f"-cache-l1-ttl={l1}",
                        "-swarm-valkey-dial-timeout=2s", "-swarm-valkey-conn-timeout=2s",
                        "-wait-for-healthcheck-interval=0", f"-routes-file={route_file}"]
                report.setdefault("commands", {})[name] = argv
                log = stack.enter_context((output / f"skipper-{name}.log").open("w"))
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                stack.callback(stop, process)

                def ready():
                    check(process.poll() is None, "Skipper exited before readiness")
                    try:
                        return request(bases[name] + "/ready")["status"] == 200
                    except OSError:
                        return False
                wait(ready, f"Skipper {name} not ready")

            def snapshot():
                return counters(exposition(metrics["a"])) | {
                    "b:" + key: value for key, value in counters(exposition(metrics["b"])).items()}

            def combined(delta):
                result = {}
                for key, value in delta.items():
                    key = key.removeprefix("b:")
                    result[key] = result.get(key, 0) + value
                return result

            def case(name, action, expected_request, expected_origin, marker,
                     status=200, cache=None, acl=None, processing=None):
                before = snapshot()
                before_stats = command_statistics(redis("INFO", "commandstats"))
                existing = redis("--scan").splitlines()
                start = len(monitor_commands(monitor_file.read_text()))
                calls = origin_count(control, marker)
                response = action()
                # All work is bounded and sequential except the explicit cold burst.
                def origin_idle():
                    with urllib.request.urlopen(control + "/events?case=" + marker, timeout=15) as result:
                        return json.load(result)["inflight"] == 0
                wait(origin_idle, f"{name}: origin still in flight")
                time.sleep(0.1)
                delta = combined(changes(before, snapshot()))
                after_stats = command_statistics(redis("INFO", "commandstats"))
                wire = monitor_commands(monitor_file.read_text())[start:]
                wire_expected = wire_operations(wire, existing)
                req = {key: value for key, value in delta.items() if key.startswith(FAMILIES[0])}
                ops = {key: value for key, value in delta.items() if key.startswith(FAMILIES[1])}
                proc = {key: value for key, value in delta.items() if key.startswith(FAMILIES[2])}
                expected_req = {metric_key(FAMILIES[0], result=result, tier=tier): count
                                for (result, tier), count in expected_request.items()}
                actual_calls = origin_count(control, marker) - calls
                observed = {"name": name, "response": response,
                            "origin_delta": actual_calls, "request_delta": req,
                            "expected_request_delta": expected_req if request_enabled else None,
                            "l2_operation_delta": ops, "wire_commands": wire,
                            "valkey_commandstats_before": before_stats,
                            "valkey_commandstats_after": after_stats,
                            "wire_expected_operations": wire_expected,
                            "processing_delta": proc, "errors": []}
                errors = observed["errors"]
                if request_enabled and req != expected_req:
                    errors.append("request outcome/tier differs from independent HTTP scenario")
                if not request_enabled and req:
                    errors.append("unexpected request feature on storage-only source")
                if storage_enabled and acl is None and ops != wire_expected:
                    errors.append("L2 client operations differ from independent MONITOR command/key oracle")
                if not storage_enabled and ops:
                    errors.append("unexpected storage feature on request-only source")
                if storage_enabled:
                    for operation in ("get", "set", "delete"):
                        metric_total = sum(value for key, value in ops.items()
                                           if json.loads(key[len(FAMILIES[1]):])["operation"] == operation)
                        old = before_stats.get(operation, {"calls": 0, "rejected": 0})
                        new = after_stats.get(operation, {"calls": 0, "rejected": 0})
                        wire_total = new["calls"] + new["rejected"] - old["calls"] - old["rejected"]
                        if metric_total != wire_total:
                            errors.append(f"{operation} metric calls {metric_total} != Valkey calls+rejections {wire_total}")
                if processing is not None and storage_enabled and proc != processing:
                    errors.append("processing errors differ from deliberate injected representation")
                if processing is None and proc:
                    errors.append("unexpected processing errors")
                if actual_calls != expected_origin:
                    errors.append(f"origin calls {actual_calls} != expected {expected_origin}")
                if not isinstance(response, list) and (
                        response["status"] != status or cache and response["cache"] != cache):
                    errors.append("HTTP status/cache result differs from expected scenario")
                if acl is not None and storage_enabled:
                    expected_ops = {metric_key(FAMILIES[1], **labels): count
                                    for labels, count in acl}
                    if ops != expected_ops:
                        errors.append("L2 error calls differ from ACL-rejected logical invocations")
                observed["status"] = "FAIL" if errors else "PASS"
                report["cases"].append(observed)
                report["errors"] += [name + ": " + error for error in errors]
                print(observed["status"] + ": " + name, flush=True)
                return response

            def fixture(path="/get", vary="", delay=0, status=200):
                marker = uuid.uuid4().hex
                configure(control, marker, vary=vary, delay=delay, status=status,
                          cache_control="public, max-age=1, stale-while-revalidate=10")
                return marker, path + "?case=" + marker

            marker, path = fixture()
            case("cold origin", lambda: request(bases["a"] + path),
                 {("origin", "none"): 1}, 1, marker, cache="MISS")
            case("fresh L1", lambda: request(bases["a"] + path),
                 {("fresh", "l1"): 1}, 0, marker, cache="HIT")
            case("fresh L2 before warming", lambda: request(bases["b"] + path),
                 {("fresh", "l2"): 1}, 0, marker, cache="HIT")
            case("cache-only L1", lambda: request(bases["a"] + path,
                 {"Cache-Control": "only-if-cached"}), {("fresh", "l1"): 1}, 0, marker, cache="HIT")
            case("HEAD L2 and silent refresh", lambda: request(bases["b"] + path, method="HEAD"),
                 {("fresh", "l2"): 1}, 0, marker, cache="HIT")
            case("bypass no-cache", lambda: request(bases["b"] + path,
                 {"Cache-Control": "no-cache"}), {("bypass", "none"): 1}, 1, marker, cache="MISS")
            case("bypass no-store", lambda: request(bases["b"] + path,
                 {"Cache-Control": "no-store"}), {("bypass", "none"): 1}, 1, marker, cache="MISS")
            server_error, server_error_path = fixture(status=503)
            case("HTTP 503 is origin not transport error", lambda: request(
                 bases["b"] + server_error_path), {("origin", "none"): 1},
                 1, server_error, status=503, cache="MISS")

            missing, missing_path = fixture()
            case("clean cache-only miss", lambda: request(bases["b"] + missing_path,
                 {"Cache-Control": "only-if-cached"}), {("cache_only_miss", "none"): 1},
                 0, missing, status=504, cache="MISS")

            varying, vary_path = fixture(vary="Accept-Language")
            case("Vary response and metadata writes", lambda: request(bases["b"] + vary_path,
                 {"Accept-Language": "en"}), {("origin", "none"): 1}, 1, varying, cache="MISS")
            case("Vary response and metadata reads", lambda: request(bases["b"] + vary_path,
                 {"Accept-Language": "en"}), {("fresh", "l2"): 1}, 0, varying, cache="HIT")
            case("L2 read provenance before L1 warming", lambda: request(bases["a"] + vary_path,
                 {"Accept-Language": "en"}), {("fresh", "l2"): 1}, 0, varying, cache="HIT")
            case("warmed Vary L1", lambda: request(bases["a"] + vary_path,
                 {"Accept-Language": "en"}), {("fresh", "l1"): 1}, 0, varying, cache="HIT")
            # Unsafe invalidation exercises independent response/metadata DEL calls.
            case("unsafe invalidation", lambda: request(bases["b"] + missing_path, method="POST"),
                 {("bypass", "none"): 1}, 1, missing)

            for metadata in (False, True):
                corrupt, corrupt_path = fixture()
                # Discover actual keys using a primed isolated UUID, not a copied hash algorithm.
                prior_keys = set(redis("--scan").splitlines())
                request(bases["b"] + corrupt_path)
                found = sorted(set(redis("--scan").splitlines()) - prior_keys)
                check(len(found) == 1, "cannot identify UUID-scoped response key")
                key = "vary:" + found[0] if metadata else found[0]
                redis("set", key, "{")
                expected_proc = {metric_key(FAMILIES[2], operation="get",
                                 purpose="metadata" if metadata else "response", stage="decode"):
                                 1 if metadata else 2}
                case("corrupt " + ("metadata" if metadata else "response"),
                     lambda p=corrupt_path: request(bases["b"] + p),
                     {("fresh", "l2"): 1} if metadata else {("error", "none"): 1},
                     0 if metadata else 1, corrupt,
                     processing=expected_proc)

            denied, denied_path = fixture()
            redis("ACL", "SETUSER", "default", "-get")
            try:
                case("ACL rejected cache-only GET", lambda: request(bases["b"] + denied_path,
                     {"Cache-Control": "only-if-cached"}), {("error", "none"): 1},
                     0, denied, status=504, cache="MISS",
                     acl=[({"operation": "get", "purpose": p, "outcome": "error"}, 1)
                          for p in ("metadata", "response")])
            finally:
                redis("ACL", "SETUSER", "default", "+get")

            denied_set, denied_set_path = fixture(vary="Accept-Language")
            redis("ACL", "SETUSER", "default", "-set")
            try:
                # Both failed Sets fall back to L1; response persistence is still attempted.
                case("ACL rejected metadata SET", lambda: request(bases["b"] + denied_set_path,
                     {"Accept-Language": "en"}), {("origin", "none"): 1},
                     1, denied_set, cache="MISS",
                     acl=[({"operation": "get", "purpose": "metadata", "outcome": "miss"}, 1),
                          ({"operation": "get", "purpose": "response", "outcome": "miss"}, 2),
                          ({"operation": "set", "purpose": "metadata", "outcome": "error"}, 1),
                          ({"operation": "set", "purpose": "response", "outcome": "error"}, 1)])
            finally:
                redis("ACL", "SETUSER", "default", "+set")
            denied_response, denied_response_path = fixture()
            redis("ACL", "SETUSER", "default", "-set")
            try:
                case("ACL rejected response SET", lambda: request(bases["b"] + denied_response_path),
                     {("origin", "none"): 1}, 1, denied_response, cache="MISS",
                     acl=[({"operation": "get", "purpose": "metadata", "outcome": "miss"}, 1),
                          ({"operation": "get", "purpose": "response", "outcome": "miss"}, 2),
                          ({"operation": "set", "purpose": "response", "outcome": "error"}, 1)])
            finally:
                redis("ACL", "SETUSER", "default", "+set")
            redis("ACL", "SETUSER", "default", "-del")
            try:
                case("ACL rejected invalidation DELETE", lambda: request(
                     bases["b"] + missing_path, method="POST"), {("bypass", "none"): 1},
                     1, missing,
                     acl=[({"operation": "get", "purpose": "metadata", "outcome": "miss"}, 1)] +
                         [({"operation": "delete", "purpose": p, "outcome": "error"}, 1)
                          for p in ("response", "metadata")])
            finally:
                redis("ACL", "SETUSER", "default", "+del")

            forged, forged_path = fixture()
            case("forged internal replay header", lambda: request(bases["b"] + forged_path,
                 {"X-Cache-Revalidate": "1"}),
                 {("bypass", "none"): 1} if args.mode == "integrated" else {},
                 1, forged, cache="MISS")

            burst, burst_path = fixture(delay=1)
            barrier = threading.Barrier(10, timeout=10)

            def burst_action():
                def fetch(_):
                    barrier.wait()
                    return request(bases["b"] + burst_path)
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                    responses = list(pool.map(fetch, range(10)))
                check(all(r["status"] == 200 for r in responses), "burst HTTP failure")
                check(all(r["body"] == responses[0]["body"] for r in responses), "burst body mismatch")
                return responses

            case("ten coalesced requests", burst_action,
                 {("origin", "none"): 1, ("coalesced", "none"): 9}, 1, burst)

            short, short_path = fixture("/short")
            request(bases["b"] + short_path)
            ready_at = time.monotonic() + 1.1
            wait(lambda: time.monotonic() >= ready_at, "short TTL age")
            case("stale L2 background 304", lambda: request(bases["b"] + short_path),
                 {("stale", "l2"): 1}, 1, short, cache="STALE")
            pipeline, pipeline_path = fixture("/rfc")
            case("pipeline replay excluded", lambda: request(bases["b"] + pipeline_path),
                 {("origin", "none"): 1}, 1, pipeline, cache="MISS")
            final_expositions = {}
            for name, url in metrics.items():
                final_expositions[name] = exposition(url)
                (output / f"final-{name}.prom").write_text(final_expositions[name])
            report["observed_family_presence"] = {
                family: any(key.startswith(family) for key in counters(
                    final_expositions["a"] + "\n" + final_expositions["b"]))
                for family in FAMILIES}
            for family in report["expected_absent_families"]:
                check(not report["observed_family_presence"][family],
                      "independent source unexpectedly contains other feature: " + family)
            report["external_probe_count"] = sum(
                len(case["response"]) if isinstance(case["response"], list) else 1
                for case in report["cases"])
            report["setup_cache_requests"] = 3  # Two corruption primes and the short-TTL prime.
            if request_enabled:
                expected_total = {metric_key(FAMILIES[0], result="origin", tier="none"): 3}
                for observed in report["cases"]:
                    for key, value in observed["expected_request_delta"].items():
                        expected_total[key] = expected_total.get(key, 0) + value
                actual_total = {}
                for exposure in final_expositions.values():
                    for key, value in counters(exposure).items():
                        if key.startswith(FAMILIES[0]):
                            actual_total[key] = actual_total.get(key, 0) + value
                report["final_request_totals"] = actual_total
                report["expected_final_request_totals"] = expected_total
                check(actual_total == expected_total,
                      "full request accounting differs from case expectations plus three setup requests")
            report["known_source_limitations"] = ([] if args.mode == "integrated" else [
                "The L1 freshness gate is unfixed; its separate regression is expected to fail.",
                "A raw X-Cache-Revalidate header suppresses request instrumentation on the request-source head; integrated authentication closes this gap.",
                "Missing feature families belong to the other independent PR, not zero observations.",
            ])
            report["gaps"] = [
                "No encode error or unknown-purpose production route: those require direct Storage callers/unit fixtures.",
                "MONITOR measures successful command transmissions, not hidden Valkey retries; ACL GET uses exact logical invocation oracle separately.",
                "No transport partition retry cardinality claim; no 24-hour retention or production workload.",
                "Individual source PRs intentionally lack the other feature and retain known L1 freshness defect.",
            ]
            report["status"] = "FAIL" if report["errors"] else "PASS"
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
        report["status"] = "ERROR"
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f'{report["status"]}: {output / "result.json"}', flush=True)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--mode", choices=("request-source", "storage-source", "integrated"), required=True)
    parser.add_argument("--output", required=True)
    raise SystemExit(run(parser.parse_args()))
