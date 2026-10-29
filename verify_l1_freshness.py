#!/usr/bin/env python3
"""Isolated two-process L1/L2 freshness regression; failure exits nonzero."""
import argparse
import base64
import contextlib
import datetime
import hashlib
import json
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from origin.mock_origin import make_servers
from verify import configure, origin_count, require


ROOT = Path(__file__).resolve().parent
PINNED_SHA = "2237f1e885a2efab21a524f8d96e8c87d58bbf69"
VALKEY_IMAGE = ("valkey/valkey:8.1.1-alpine@sha256:"
                "6a57d58c0a37cf7acc3045ac0fd6dd91be339774106ab4d9ca088013a096a99f")
L1_TTL = 2
SWR = 1
TTL = 30
SIE = 30


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=180).strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url, headers=None):
    req = urllib.request.Request(url, headers={"Host": "freshness.test", **(headers or {})})
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read().decode()
        return {"status": response.status,
                "cache_status": response.headers.get("X-Cache-Status"),
                "age": response.headers.get("Age"),
                "body": json.loads(body) if body else None}


def wait_ready(url, process, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require(process.poll() is None, f"Skipper exited with {process.returncode}")
        try:
            if request(url)["status"] == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise TimeoutError(f"Skipper readiness timed out: {url}")


def parse_metrics(text):
    return {name: float(value) for name, value in re.findall(
        r'^skipper_custom_total\{key="(cache\.[^"]+)"\} (\S+)$', text, re.MULTILINE)}


def metrics(url):
    with urllib.request.urlopen(url + "/metrics", timeout=5) as response:
        return parse_metrics(response.read().decode())


def delta(before, after):
    return {key: after.get(key, 0) - before.get(key, 0)
            for key in before.keys() | after.keys()
            if after.get(key, 0) != before.get(key, 0)}


def regression_errors(observations):
    errors = []
    for name in ("a_only_if_cached", "a_normal"):
        observed = observations[name]
        response = observed["response"]
        if (response["status"] != 200 or response["cache_status"] != "HIT"
                or not response["body"] or response["body"].get("version") != "B"
                or observed["origin_delta"] != 0):
            errors.append(f"{name}: expected HTTP 200 / B / HIT / origin_delta=0; "
                          f"actual={json.dumps(observed, sort_keys=True)}")
    return errors


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "status": "ERROR", "observations": {},
              "expected": "A serves fresh shared B as HTTP 200/HIT with zero origin calls",
              "budgets_seconds": {"l1_ttl": L1_TTL, "swr": SWR, "ttl": TTL, "sie": SIE}}
    container = "skipper-l1-freshness-" + uuid.uuid4().hex[:12]
    try:
        checkout = Path(args.checkout).resolve()
        sha = command("git", "-C", str(checkout), "rev-parse", "HEAD")
        require(sha == args.sha, f"checkout SHA {sha} != requested SHA {args.sha}")
        require(not command("git", "-C", str(checkout), "status", "--porcelain"),
                "Skipper source must be clean (including before building)")
        binary = output / "skipper"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/skipper"],
                       cwd=checkout, check=True, timeout=180)
        report["provenance"] = {
            "skipper_sha": sha,
            "skipper_describe": command("git", "-C", str(checkout), "describe", "--always", "--tags"),
            "skipper_version": command(str(binary), "-version"),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "build_info": command("go", "version", "-m", str(binary)),
            "suite_sha": command("git", "rev-parse", "HEAD"),
            "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "origin_sha256": hashlib.sha256(
                (ROOT / "origin/mock_origin.py").read_bytes()).hexdigest(),
            "runner": platform.platform(), "python": sys.version,
            "skipper_image": None, "build": "native go build ./cmd/skipper, unmodified source",
            "docker_server": command("docker", "info", "--format", "{{.OSType}}/{{.Architecture}} {{.ServerVersion}}"),
        }
        subprocess.run(["docker", "pull", args.valkey_image], check=True, timeout=180)
        image = json.loads(command("docker", "image", "inspect", args.valkey_image))[0]
        report["provenance"]["valkey_image"] = {
            key: image[key] for key in ("Id", "RepoDigests", "Architecture", "Os")}
        # A fresh loopback-only container avoids existing suites, keys and fixed ports.
        with contextlib.ExitStack() as stack:
            command("docker", "run", "--detach", "--name", container,
                    "--publish", "127.0.0.1::6379", args.valkey_image,
                    "valkey-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "rm", "--force", container)
            valkey_port = command("docker", "port", container, "6379/tcp").rsplit(":", 1)[1]
            deadline = time.monotonic() + 10
            while command("docker", "exec", container, "valkey-cli", "ping") != "PONG":
                require(time.monotonic() < deadline, "Valkey readiness timed out")
                time.sleep(0.05)
            report["provenance"]["valkey_version"] = command(
                "docker", "exec", container, "valkey-server", "--version")
            servers = make_servers(http_port=0, control_port=0)
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            origin = f"http://127.0.0.1:{servers[0].server_port}"
            control = f"http://127.0.0.1:{servers[1].server_port}"
            route = (f'freshness: Path("/get") -> cache("{TTL}s", "1s", "{SWR}s", "{SIE}s") '
                     f'-> "{origin}";\n'
                     'ready: Path("/ready") -> status(200) -> <shunt>;\n')
            route_file = output / "routes.eskip"
            route_file.write_text(route)
            report["route"] = route
            urls, metric_urls = {}, {}
            for name, warm_ttl in (("a", L1_TTL), ("b", 0)):
                port, metric_port = free_port(), free_port()
                urls[name] = f"http://127.0.0.1:{port}"
                metric_urls[name] = f"http://127.0.0.1:{metric_port}"
                argv = [str(binary), f"-address=127.0.0.1:{port}",
                        f"-support-listener=127.0.0.1:{metric_port}",
                        "-metrics-flavour=prometheus", "-proxy-preserve-host",
                        "-enable-swarm", "-enable-l2-cache",
                        f"-swarm-valkey-urls=127.0.0.1:{valkey_port}",
                        "-swarm-valkey-dial-timeout=2s", "-swarm-valkey-conn-timeout=2s",
                        f"-cache-l1-ttl={warm_ttl}s", "-wait-for-healthcheck-interval=0",
                        f"-routes-file={route_file}"]
                report.setdefault("commands", {})[name] = argv
                log = stack.enter_context((output / f"skipper-{name}.log").open("w"))
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                stack.callback(stop_process, process)
                wait_ready(urls[name] + "/ready", process)
            marker = uuid.uuid4().hex
            path = "/get?case=" + marker
            report["case"] = marker
            configure(control, marker, version="A", vary="",
                      cache_control=f"public, max-age={TTL}")

            def observe(name, instance, headers=None):
                count = origin_count(control, marker)
                before = metrics(metric_urls[instance])
                response = request(urls[instance] + path, headers)
                observed = {"response": response,
                            "origin_delta": origin_count(control, marker) - count,
                            "origin_total": origin_count(control, marker),
                            "metric_delta": delta(before, metrics(metric_urls[instance]))}
                report["observations"][name] = observed
                return observed

            def check(name, instance, version, status, calls, headers=None):
                observed = observe(name, instance, headers)
                response = observed["response"]
                require(response["status"] == 200 and isinstance(response["body"], dict)
                        and response["body"].get("version") == version
                        and response["cache_status"] == status and observed["origin_delta"] == calls,
                        f"setup/control {name} failed: {observed}")
                return observed

            primed_after = time.monotonic()
            check("a_prime", "a", "A", "MISS", 1)
            warm = check("a_warm", "a", "A", "HIT", 0)
            require(warm["metric_delta"].get("cache.l1_hit") == 1,
                    "A warm control did not demonstrate an L1 hit")
            warmed_before = time.monotonic()
            old_shared = check("b_shared_old", "b", "A", "HIT", 0)
            require(old_shared["metric_delta"].get("cache.l2_hit") == 1,
                    "B old shared control did not demonstrate an L2 hit")

            # Do not touch A in this interval: SWR traffic would refresh its L1.
            # This deadline is derived from the confirmed warm hit, not a guessed sleep.
            target = warmed_before + L1_TTL + SWR + 0.1
            while time.monotonic() < target:
                time.sleep(min(0.05, target - time.monotonic()))
            report["old_l1_age_lower_bound_seconds"] = time.monotonic() - warmed_before
            require(time.monotonic() - primed_after < L1_TTL + SIE - 1,
                    "timing budget exceeded L1 retention; cannot classify reproduction")
            configure(control, marker, version="B", vary="",
                      cache_control=f"public, max-age={TTL}")
            # Explicit synchronous refresh avoids background revalidation and the
            # malformed cold coalesce request. Response() still writes through L2.
            check("b_refresh", "b", "B", "MISS", 1, {"Cache-Control": "no-cache"})
            fresh_shared = check("b_shared_fresh", "b", "B", "HIT", 0)
            require(fresh_shared["metric_delta"].get("cache.l2_hit") == 1,
                    "B fresh shared control did not demonstrate an L2 hit")
            keys = command("docker", "exec", container, "valkey-cli", "--scan").splitlines()
            entries = {}
            for key in keys:
                raw = command("docker", "exec", container, "valkey-cli", "--raw", "get", key)
                entries[key] = json.loads(raw)
            report["l2_before_probes"] = entries
            require(len(entries) == 1, f"expected one isolated L2 entry, got {len(entries)}")
            shared = next(iter(entries.values()))
            shared_body = json.loads(base64.b64decode(shared["Payload"], validate=True))
            created = datetime.datetime.fromisoformat(shared["CreatedAt"].replace("Z", "+00:00"))
            shared_age = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()
            require(shared_body["version"] == "B" and shared["StatusCode"] == 200
                    and 0 <= shared_age < shared["TTL"] / 1e9 - 5,
                    "raw L2 entry is not demonstrably fresh B with five-second probe budget")
            report["shared_l2_age_seconds_before_probes"] = shared_age
            report["old_l1_age_upper_bound_seconds"] = time.monotonic() - primed_after
            require(time.monotonic() - primed_after < L1_TTL + SIE - 1,
                    "setup consumed L1 retention budget")
            # Keep both regressions visible even when the first one fails.
            observe("a_only_if_cached", "a", {"Cache-Control": "only-if-cached"})
            observe("a_normal", "a")
            report["errors"] = regression_errors(report["observations"])
            report["status"] = "FAIL" if report["errors"] else "PASS"
            report["origin_calls"] = origin_count(control, marker)
            with urllib.request.urlopen(control + "/events?case=" + marker, timeout=5) as response:
                report["origin_events"] = json.load(response)
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as error:
        report["status"] = "ERROR"
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("provenance", "l2_before_probes", "commands")}, indent=2))
    print(f'Result: {report["status"]}; {output / "result.json"}', flush=True)
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, help="clean pinned Skipper source checkout")
    parser.add_argument("--sha", default=PINNED_SHA, help="required exact checkout SHA")
    parser.add_argument("--valkey-image", default=VALKEY_IMAGE)
    parser.add_argument("--output", default=".runtime/l1-freshness")
    raise SystemExit(run(parser.parse_args()))
