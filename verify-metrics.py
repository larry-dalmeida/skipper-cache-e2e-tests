#!/usr/bin/env python3
"""Check every dashboard PromQL target, both directly and through Grafana."""
import argparse
from collections import Counter
import datetime
import json
import math
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def stack_urls(root):
    """Grafana and Prometheus base URLs: `.runtime/urls.txt` (minikube service ports) when present."""
    urls = {"grafana": "http://127.0.0.1:3000", "prometheus": "http://127.0.0.1:9090"}
    path = root / ".runtime" / "urls.txt"
    if path.exists():
        for line in path.read_text().splitlines():
            label, _, url = line.partition(":")
            if label.strip() in urls and url.strip():
                urls[label.strip()] = url.strip().rstrip("/")
    return urls


# Only these missing event counters are expected for each workload. All other
# panels, including missing scrape/traffic metrics, remain mandatory.
UNEXERCISED_EVENTS = {
    "smoke": {("cache-performance-local", 19), ("cache-performance-local", 60)},
    "origin-stale": {("cache-performance-local", 60)},
    "valkey-kill": {("cache-performance-local", 60)},
    "skipper-kill": {("cache-performance-local", 19), ("cache-performance-local", 60)},
    "soak": {("cache-performance-local", 19), ("cache-performance-local", 60)},
    "strict": set(),
}
VARIABLE = re.compile(r"\$\{([a-zA-Z_]\w*)(?::(\w+))?\}|\$([a-zA-Z_]\w*)")


def dashboard_variables(dashboard):
    values = {"__rate_interval": "1m", "__interval": "1m",
              "__range": "15m", "__range_s": "900", "__interval_ms": "60000"}
    start = dashboard.get("time", {}).get("from", "now-15m")
    match = re.fullmatch(r"now-(\d+)([smhdw])", start)
    if match:
        values["__range"] = start[4:]
        values["__range_s"] = str(int(match[1]) * {"s": 1, "m": 60, "h": 3600,
                                                              "d": 86400, "w": 604800}[match[2]])
    for variable in dashboard.get("templating", {}).get("list", []):
        selected = variable.get("current", {}).get("value")
        selected = selected if isinstance(selected, list) else [selected]
        all_selected = "$__all" in selected or selected == [None]
        if all_selected and variable.get("includeAll"):
            if variable.get("allValue"):
                values[variable["name"]] = variable["allValue"]
                continue
            selected = [option["value"] for option in variable.get("options", [])
                        if option.get("value") not in (None, "$__all")]
            if not selected and variable.get("type") == "custom":
                selected = [item.strip() for item in variable.get("query", "").split(",")]
        elif selected == [None]:
            selected = [option["value"] for option in variable.get("options", [])
                        if option.get("selected")]
        if not selected or any(value is None for value in selected):
            raise ValueError(f"No default/All value for dashboard variable {variable['name']}")
        values[variable["name"]] = selected
    return values


def expand_query(expression, values):
    def substitute(match):
        name, format_, bare = match.groups()
        name = name or bare
        if name not in values:
            raise ValueError(f"Unresolved dashboard variable: {name}")
        value = values[name]
        if isinstance(value, list):
            if format_ not in (None, "regex", "raw", "csv"):
                raise ValueError(f"Unsupported dashboard variable format: {format_}")
            if format_ in ("raw", "csv"):
                return ",".join(map(str, value))
            # Escape regex literals, then quote them for PromQL string syntax.
            return "|".join(re.escape(str(item)) for item in value).replace("\\", "\\\\")
        return str(value)

    return VARIABLE.sub(substitute, expression)


def sample_status(expression, samples, allow_missing=False):
    if not samples:
        return "NOT EXERCISED" if allow_missing else "FAIL"
    finite = [math.isfinite(float(sample["value"][1])) for sample in samples]
    if expression.lstrip().startswith("histogram_quantile("):
        # Inactive bucket sets yield NaN; at least one active set must be finite.
        healthy = any(finite) and not any(math.isinf(float(sample["value"][1]))
                                         for sample in samples)
    else:
        healthy = all(finite)
    if expression.lstrip().startswith("up{"):
        healthy = healthy and all(float(sample["value"][1]) == 1 for sample in samples)
    return "PASS" if healthy else "FAIL"


def query_status(expression, direct, proxy, allow_missing=False):
    if direct.get("status") != "success" or proxy.get("status") != "success":
        raise ValueError(f"Query failed: {expression}")
    direct_status = sample_status(expression, direct["data"]["result"], allow_missing)
    proxy_status = sample_status(expression, proxy["data"]["result"], allow_missing)
    status = proxy_status if direct_status == proxy_status else "FAIL"
    return status, direct_status, proxy_status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=UNEXERCISED_EVENTS, default="smoke",
                        help="Missing-event allowlist; strict requires every panel")
    parser.add_argument("--sha", required=True, help="Exact running Skipper source SHA")
    parser.add_argument("--output", default=".runtime/metrics.json")
    parser.add_argument("--wait-seconds", type=float, default=45,
                        help="Retry window per required query; 0 for an already-warmed smoke run")
    args = parser.parse_args()
    if not math.isfinite(args.wait_seconds) or args.wait_seconds < 0:
        parser.error("--wait-seconds must be finite and nonnegative")
    root = Path(__file__).resolve().parent
    urls = stack_urls(root)
    grafana, prometheus = urls["grafana"], urls["prometheus"]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    panels = [(dashboard, panel) for path in sorted((root / "dashboards").glob("*.json"))
              for dashboard in [json.loads(path.read_text())] for panel in dashboard["panels"]]
    datasources = [d for d in get(grafana + "/api/datasources")
                   if d["type"] == "prometheus"]
    if len(datasources) != 1:
        raise SystemExit("Expected one local Prometheus datasource")
    uid = datasources[0]["uid"]
    results = []
    for dashboard, panel in panels:
        variables = dashboard_variables(dashboard)
        for target in panel.get("targets", []):
            expr = expand_query(target["expr"], variables)
            deadline = time.monotonic() + args.wait_seconds
            optional = (dashboard["uid"], panel["id"]) in UNEXERCISED_EVENTS[args.scenario]
            while True:
                query = urllib.parse.urlencode({"query": expr})
                direct = get(prometheus + "/api/v1/query?" + query)
                proxy = get(grafana + "/api/datasources/proxy/uid/" +
                            uid + "/api/v1/query?" + query)
                samples = proxy["data"]["result"]
                status, direct_status, proxy_status = query_status(expr, direct, proxy, optional)
                if status != "FAIL" or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            results.append({"dashboard": dashboard["uid"], "panel_id": panel["id"],
                            "panel": panel["title"], "query": expr,
                            "direct_status": direct_status, "proxy_status": proxy_status,
                            "status": status, "samples": samples})
            print(f"{status}: {panel['title']}: {len(samples)} series")
    with urllib.request.urlopen("http://127.0.0.1:9911/metrics", timeout=10) as response:
        exposition = response.read().decode()
    wanted = ("skipper_cache_", "skipper_custom_duration_seconds_count{key=\"cache.",
              "skipper_custom_duration_seconds_sum{key=\"cache.")
    output.with_name(output.stem + "-samples.txt").write_text(
        "\n".join(line for line in exposition.splitlines()
                  if line.startswith(wanted)) + "\n")
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "skipper_sha": args.sha, "scenario": args.scenario,
              "units": "per-panel: req/s for outcome rates, seconds for latency, bytes for L1",
              "resolved_rate_interval": "1m (4 x configured 5s scrape interval minimum exceeded)",
              "panels": results}
    flat_query = urllib.parse.urlencode({
        "query": 'skipper_custom_total{job="skipper-local",key=~"cache\\\\.(hit|miss|stale|l2_get_error|l2_set_fallback|coalesce_error|coalesce_bypass_error|reval_error|reval_dropped|lru_eviction|lru_oversized)"}'})
    report["flat_counter_samples"] = get(
        prometheus + "/api/v1/query?" + flat_query)["data"]["result"]
    expected_keys = {"cache." + key for key in (
        "hit", "miss", "stale", "l2_get_error", "l2_set_fallback",
        "coalesce_error", "coalesce_bypass_error", "reval_error", "reval_dropped",
        "lru_eviction", "lru_oversized")}
    observed_keys = {s["metric"]["key"] for s in report["flat_counter_samples"]}
    report["absent_flat_counters_not_exercised"] = sorted(expected_keys - observed_keys)
    label_query = urllib.parse.urlencode({
        "query": 'skipper_cache_result_total{job="skipper-local",instance=~".*"}'})
    labels = get(prometheus + "/api/v1/query?" + label_query)["data"]["result"]
    report["observed_result_code_classes"] = [
        {"result": sample["metric"]["result"],
         "code_class": sample["metric"]["code_class"]}
        for sample in labels]
    present = {sample["metric"]["result"] for sample in labels}
    report["missing_outcomes"] = sorted({"hit", "miss", "l1_hit", "l2_hit"} - present)
    if report["missing_outcomes"]:
        print("NOT EXERCISED / MISSING (never synthesized as zero): " +
              ", ".join(report["missing_outcomes"]))
    report["counts"] = dict(Counter(result["status"] for result in results))
    output.write_text(json.dumps(report, indent=2) + "\n")
    print("Metrics: " + ", ".join(f"{report['counts'].get(status, 0)} {status}"
                                  for status in ("PASS", "NOT EXERCISED", "FAIL")))
    raise SystemExit(1 if any(r["status"] == "FAIL" for r in results) else 0)
