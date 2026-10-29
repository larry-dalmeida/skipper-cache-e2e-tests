#!/usr/bin/env python3
"""Bounded local acceptance of real emitted metrics and repository dashboard PromQL."""
import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from origin.mock_origin import make_servers
from verify import configure, origin_count
from verify_l1_freshness import (
    VALKEY_IMAGE, command, free_port, request, require, stop_process, wait_ready,
)

spec = importlib.util.spec_from_file_location("verify_metrics", ROOT / "verify-metrics.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)
PROMETHEUS_IMAGE = "prom/prometheus:v2.55.1"


def text(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.read().decode()


def samples(exposition, metric):
    result = []
    for line in exposition.splitlines():
        match = re.fullmatch(re.escape(metric) + r'(\{[^}]*\})? ([^\s]+)', line)
        if match:
            labels = dict(re.findall(r'(\w+)="([^"]*)"', match[1] or ""))
            result.append({"labels": labels, "value": float(match[2])})
    return result


def wait_for(predicate, message, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    relay = output / "relay"
    relay.mkdir(exist_ok=True)
    report = {"status": "ERROR", "sha": args.sha,
              "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "scope": "local Prometheus query execution; no Grafana rendering/proxy claim",
              "scraping": "native Prometheus directly scrapes actual loopback Skipper and origin listeners",
              "cases": [], "errors": []}
    try:
        checkout = Path(args.checkout).resolve()
        require(command("git", "-C", str(checkout), "rev-parse", "HEAD") == args.sha,
                "candidate source SHA mismatch")
        require(not command("git", "-C", str(checkout), "status", "--porcelain"),
                "candidate source must remain clean")
        binary = output / "skipper"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/skipper"],
                       cwd=checkout, timeout=180, check=True)
        report["provenance"] = {
            "version": command(str(binary), "-version"),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "build_info": command("go", "version", "-m", str(binary)),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "valkey_image": json.loads(command("docker", "image", "inspect", VALKEY_IMAGE))[0]["RepoDigests"],
            "prometheus_image": json.loads(command("docker", "image", "inspect", PROMETHEUS_IMAGE))[0]["RepoDigests"],
            "prometheus_native_version": command(str(Path(args.prometheus_binary).resolve()), "--version"),
            "prometheus_native_sha256": hashlib.sha256(Path(args.prometheus_binary).read_bytes()).hexdigest(),
            "dashboard_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (ROOT / "dashboards").glob("*.json")},
        }
        with contextlib.ExitStack() as stack:
            valkey_name = "dashboard-valkey-" + uuid.uuid4().hex[:12]
            command("docker", "run", "--detach", "--name", valkey_name,
                    "--publish", "127.0.0.1::6379", VALKEY_IMAGE,
                    "valkey-server", "--save", "", "--appendonly", "no")
            stack.callback(command, "docker", "rm", "--force", valkey_name)
            valkey_port = command("docker", "port", valkey_name, "6379/tcp").rsplit(":", 1)[1]
            wait_for(lambda: command("docker", "exec", valkey_name, "valkey-cli", "ping") == "PONG",
                     "Valkey not ready")
            servers = make_servers(http_port=0, control_port=0)
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            origin = f"http://127.0.0.1:{servers[0].server_port}"
            control = f"http://127.0.0.1:{servers[1].server_port}"
            http_port, metrics_port = free_port(), free_port()
            base, metric_url = f"http://127.0.0.1:{http_port}", f"http://127.0.0.1:{metrics_port}/metrics"
            routes = []
            cases = []
            for dispatch in ("direct", "pipeline"):
                for outcome in ("success", "503", "disconnect"):
                    route = f"{dispatch}__{outcome}"
                    filter_ = 'cache("1s", "1s", "10s", "30s")' if dispatch == "direct" else "cache()"
                    routes.append(f'{route}: Path("/{route}") -> {filter_} -> "{origin}";')
                    cases.append((dispatch, outcome, route))
            routes.extend([
                f'noncache: Path("/noncache") -> "{origin}";',
                f'uncacheable: Path("/uncacheable") -> cache() -> "{origin}";',
                f'failed: Path("/failed") -> "{origin}";',
                'ready: Path("/ready") -> healthcheck() -> status(200) -> <shunt>;',
            ])
            route_file = output / "routes.eskip"
            route_file.write_text("\n".join(routes) + "\n")
            argv = [str(binary), f"-address=127.0.0.1:{http_port}",
                    f"-support-listener=127.0.0.1:{metrics_port}", "-metrics-flavour=prometheus",
                    "-proxy-preserve-host", "-serve-route-counter", "-serve-route-metrics",
                    "-enable-swarm", "-enable-l2-cache", f"-swarm-valkey-urls=127.0.0.1:{valkey_port}",
                    "-swarm-valkey-dial-timeout=2s", "-swarm-valkey-conn-timeout=2s",
                    "-cache-l1-ttl=200ms", "-wait-for-healthcheck-interval=0",
                    f"-routes-file={route_file}"]
            report["skipper_command"] = argv
            log = stack.enter_context((output / "skipper.log").open("w"))
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            stack.callback(stop_process, process)
            wait_ready(base + "/ready", process)
            initial = text(metric_url)
            (output / "initial.prom").write_text(initial)
            require(not samples(initial, "skipper_cache_first_hit_seconds"),
                    "first-hit should be absent before cache traffic")
            prom_config = output / "prometheus.yml"
            prom_config.write_text(
                "global:\n  scrape_interval: 1s\nscrape_configs:\n"
                "  - job_name: skipper-local\n"
                f"    static_configs:\n      - targets: ['127.0.0.1:{metrics_port}']\n"
                "        labels: {run_id: acceptance, instance: candidate}\n"
                "  - job_name: mock-origin\n"
                f"    static_configs:\n      - targets: ['127.0.0.1:{servers[1].server_port}']\n"
                "        labels: {run_id: acceptance, instance: origin}\n")
            prom_port = free_port()
            prom_log = stack.enter_context((output / "prometheus.log").open("w"))
            prom_argv = [str(Path(args.prometheus_binary).resolve()),
                         f"--config.file={prom_config}", f"--storage.tsdb.path={output / 'tsdb'}",
                         f"--web.listen-address=127.0.0.1:{prom_port}"]
            report["prometheus_command"] = prom_argv
            prom_process = subprocess.Popen(prom_argv, stdout=prom_log, stderr=subprocess.STDOUT)
            stack.callback(stop_process, prom_process)
            prom = f"http://127.0.0.1:{prom_port}"

            def prom_ready():
                try:
                    return "Ready" in text(prom + "/-/ready")
                except OSError:
                    return False

            wait_for(prom_ready, "Prometheus readiness timed out", 20)

            def query(expression):
                report["last_query"] = expression
                params = {"query": expression, "timeout": "20s"}
                if "evaluation_time" in report:
                    params["time"] = str(report["evaluation_time"])
                with urllib.request.urlopen(
                        prom + "/api/v1/query?" + urllib.parse.urlencode(
                            params), timeout=30) as response:
                    return json.load(response)

            wait_for(lambda: len(query('up == 1')["data"]["result"]) == 2,
                     "real relay sources were not scraped")
            time.sleep(2.2)
            report["absent_before_traffic"] = query("skipper_cache_first_hit_seconds")
            require(not report["absent_before_traffic"]["data"]["result"],
                    "unobserved first-hit metric must not be synthesized as zero")
            dashboard = json.loads((ROOT / "dashboards/cache-performance.json").read_text())
            variables = vm.dashboard_variables(dashboard)
            report["pre_traffic_client_queries"] = {}
            for panel in dashboard["panels"]:
                if panel["id"] in (44, 48, 50, 55):
                    expression = vm.expand_query(panel["targets"][0]["expr"], variables)
                    result = query(expression)
                    report["pre_traffic_client_queries"][str(panel["id"])] = result
                    if panel["id"] == 44:
                        require(len(result["data"]["result"]) == 1 and
                                float(result["data"]["result"][0]["value"][1]) == 0,
                                "initialized client counter should report observed zero RPS before traffic")
                    else:
                        require(not result["data"]["result"],
                                "zero client denominator must not yield success-shaped offload/ratio")
            cc = "public, max-age=1, stale-while-revalidate=10, stale-if-error=30"
            report["client_counter_probes"] = []
            def client_count():
                values = [s for s in samples(text(metric_url), "skipper_custom_total")
                          if s["labels"].get("key") == "client.requests"]
                require(len(values) == 1, "client.requests must be initialized and scalar")
                return values[0]["value"]
            require(client_count() == 0, "healthcheck readiness must not count as external client")
            for path_, cache_control, status, headers in (
                    ("/noncache", cc, 200, {}),
                    ("/uncacheable", "no-store", 200, {}),
                    ("/failed", cc, 503, {}),
                    ("/absent", cc, 200, {}),
                    ("/noncache", cc, 200, {"X-Cache-Revalidate": "1"})):
                marker = uuid.uuid4().hex
                configure(control, marker, cache_control=cache_control, status=status, vary="")
                before = client_count()
                req = urllib.request.Request(base + path_ + "?case=" + marker, headers=headers)
                try:
                    response = urllib.request.urlopen(req, timeout=15)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    actual_status = response.status
                    response.read()
                wait_for(lambda: client_count() == before + 1,
                         f"external {path_} must increment client counter exactly once")
                require(actual_status == (404 if path_ == "/absent" else status),
                        f"unexpected status for {path_}: {actual_status}")
                report["client_counter_probes"].append({
                    "path": path_, "status": actual_status, "headers": headers,
                    "counter_before": before, "counter_after": client_count()})
            occurrences = {}
            for dispatch, outcome, route in cases + cases:
                occurrences[route] = occurrences.get(route, 0) + 1
                occurrence = occurrences[route]
                marker = uuid.uuid4().hex
                path = f"/{route}?case={marker}"
                configure(control, marker, version="A", vary="", cache_control=cc)
                cold = request(base + path)
                warm = request(base + path)
                require(cold["status"] == warm["status"] == 200 and warm["cache_status"] == "HIT",
                        f"cold/warm setup failed for {route}")
                age_start = time.monotonic()
                configure(control, marker, version="B", vary="", cache_control=cc,
                          status=503 if outcome == "503" else 200,
                          disconnect=outcome == "disconnect")
                target = age_start + 1.1
                while time.monotonic() < target:
                    time.sleep(min(0.05, target - time.monotonic()))
                stale = request(base + path)
                require(stale["cache_status"] == "STALE", f"{route} did not trigger refresh: {stale}")

                def terminal():
                    series = samples(text(metric_url), "skipper_cache_route_reval_total")
                    return any(s["labels"].get("route") == route and
                               s["labels"].get("outcome") in ("success", "failed") and
                               s["value"] >= occurrence for s in series)

                wait_for(terminal, f"{route} background refresh did not terminate")
                exposure = text(metric_url)
                (output / (route + f"-{occurrence}.prom")).write_text(exposure)
                reval = [s for s in samples(exposure, "skipper_cache_route_reval_total")
                         if s["labels"].get("route") == route]
                durations = [s for s in samples(exposure, "skipper_cache_origin_duration_seconds_count")
                             if s["labels"].get("route") == route]
                case = {"dispatch": dispatch, "outcome": outcome, "route": route,
                        "occurrence": occurrence,
                        "cold": cold, "warm": warm, "stale": stale,
                        "origin_calls": origin_count(control, marker),
                        "refresh_metrics": reval, "origin_histogram_counts": durations}
                report["cases"].append(case)
                expected = "success" if outcome == "success" else "failed"
                require(any(s["labels"].get("outcome") == expected and s["value"] == occurrence for s in reval),
                        f"{route} missing expected terminal {expected}")
                require(any(s["labels"].get("outcome") == "started" and s["value"] == occurrence for s in reval),
                        f"{route} missing exactly one started")
                status_class = ("2xx" if outcome == "success" else
                                "error" if outcome == "disconnect" and dispatch == "direct" else "5xx")
                require(any(s["labels"].get("dispatch") == dispatch and
                            s["labels"].get("status_class") == status_class and s["value"] >= 1
                            for s in durations), f"{route} missing {dispatch}/{status_class}")
            # Ensure two scrapes after the final observations; no arbitrary load window.
            target_time = time.time() + 2.2
            wait_for(lambda: time.time() >= target_time, "post-workload scrape deadline", 5)
            final = text(metric_url)
            (output / "final.prom").write_text(final)
            (output / "origin-final.prom").write_text(text(control + "/metrics"))
            report["evaluation_time"] = time.time()
            report["panels"] = []
            for file in sorted((ROOT / "dashboards").glob("*.json")):
                dashboard = json.loads(file.read_text())
                variables = vm.dashboard_variables(dashboard)
                for panel in dashboard["panels"]:
                    for target in panel.get("targets", []):
                        expression = vm.expand_query(target["expr"], variables)
                        response = query(expression)
                        require(response.get("status") == "success", f"query failed: {expression}")
                        values = response["data"]["result"]
                        status = vm.sample_status(expression, values, allow_missing=True)
                        report["panels"].append({
                            "dashboard": dashboard["uid"], "panel_id": panel["id"],
                            "title": panel["title"], "unit": panel.get("fieldConfig", {}).get("defaults", {}).get("unit"),
                            "expression": expression, "status": status, "response": response,
                        })
            report["contract_queries"] = {
                expr: query(expr) for expr in (
                    'sum by (outcome) (skipper_cache_route_reval_total)',
                    'sum(skipper_cache_route_reval_total{outcome="started"})',
                    'sum(skipper_cache_route_reval_total{outcome=~"success|failed"})',
                    'sum by (dispatch,status_class) (skipper_cache_origin_duration_seconds_count)',
                    'sum by (result) (skipper_cache_route_result_total)',
                    'sum by (cache_control) (skipper_cache_origin_cache_control_total)',
                    'sum(skipper_serve_route_count)',
                    'sum(mock_origin_requests_total)',
                    'skipper_custom_total{key="client.requests"}',
                    'skipper_cache_first_hit_seconds',
                )}
            required = {44, 45, 46, 47, 48, 49, 7, 8, 9, 11, 12, 13, 14,
                        15, 16, 18, 19, 20, 24, 25, 50, 52, 53, 54, 55,
                        57, 58, 59, 61, 62}
            report["errors"] += [
                f'panel {p["panel_id"]} {p["title"]}: {p["status"]}'
                for p in report["panels"] if p["dashboard"] == "cache-performance-local"
                and p["panel_id"] in required and p["status"] != "PASS"]
            refresh_panel = next(p for p in report["panels"] if
                                 p["dashboard"] == "cache-performance-local" and p["panel_id"] == 53)
            for value in refresh_panel["response"]["data"]["result"]:
                require(float(value["value"][1]) > 0,
                        f"refresh outcome rate not exercised positively: {value}")
            origin_panel = next(p for p in report["panels"] if
                                p["dashboard"] == "cache-performance-local" and p["panel_id"] == 54)
            exercised_latencies = [value for value in origin_panel["response"]["data"]["result"]
                                   if value["metric"].get("family") in ("direct", "pipeline")]
            require(len(exercised_latencies) == 5 and all(math.isfinite(float(value["value"][1]))
                        for value in exercised_latencies),
                    "fault/success origin latency series not all finite after repeated observations")
            report["unexercised_latency_series"] = [
                value for value in origin_panel["response"]["data"]["result"]
                if value["metric"].get("family") not in ("direct", "pipeline")
                and not math.isfinite(float(value["value"][1]))]
            def scalar(expression):
                result = report["contract_queries"][expression]["data"]["result"]
                require(len(result) == 1, f"expected one actual sample: {expression}")
                return float(result[0]["value"][1])
            require(scalar('sum(skipper_cache_route_reval_total{outcome="started"})') == 12,
                    "expected 12 started refreshes")
            require(scalar('sum(skipper_cache_route_reval_total{outcome=~"success|failed"})') == 12,
                    "started must equal terminal success+failed, not their sum as attempts")
            require(scalar('sum(mock_origin_requests_total)') == 28,
                    "expected exactly 28 real fixture calls including non-cache probes")
            require(scalar('skipper_custom_total{key="client.requests"}') == 41,
                    "expected 36 external cache requests + five non-cache/error/spoof probes")
            cc_samples = report["contract_queries"][
                'sum by (cache_control) (skipper_cache_origin_cache_control_total)']["data"]["result"]
            require(len(cc_samples) == 5 and all(
                float(s["value"][1]) == (1 if s["metric"]["cache_control"] == "no-store" else 20)
                for s in cc_samples),
                "each directive must count once per actual cache-dispatched HTTP response; no-store once")
            report["denominator_checks"] = {
                "external_requests": 41,
                "pipeline_internal_requests": 13,
                "readiness_requests": 1,
                "observed_listener_requests": scalar('sum(skipper_serve_route_count)'),
                "observed_origin_calls": 28,
                "observed_client_requests": scalar('skipper_custom_total{key="client.requests"}'),
                "refresh_attempts_started": 12,
                "refresh_terminal": 12,
                "cache_control_each_directive": 20,
                "scope_warning": "Origin numerator is all callers, not cache attribution. Client denominator excludes authenticated replays and explicit healthcheck() requests, not arbitrary health-like paths.",
            }
            require(report["denominator_checks"]["observed_listener_requests"] == 55,
                    "listener denominator differs from exact external+pipeline+readiness accounting")
            report["gaps"] = [
                "No Grafana UI/rendering or datasource-proxy validation; actual Prometheus queries only.",
                "No eviction/queue-drop load; absent event series explicitly NOT EXERCISED, not zero.",
                "Synthetic routes use direct/pipeline families, not retail locale taxonomy; retail route panels may be unexercised.",
                "No production dashboards or resources changed.",
            ]
            report["status"] = "FAIL" if report["errors"] else "PASS"
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as error:
        report["status"] = "ERROR"
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        (output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("provenance", "panels", "contract_queries", "skipper_command", "cases")}, indent=2))
    print(output / "acceptance.json")
    return {"PASS": 0, "FAIL": 1, "ERROR": 2}[report["status"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prometheus-binary", default=str(
        ROOT / ".runtime/prometheus-native/prometheus-2.55.1.darwin-arm64/prometheus"))
    raise SystemExit(run(parser.parse_args()))
