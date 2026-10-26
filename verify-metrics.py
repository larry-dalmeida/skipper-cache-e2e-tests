#!/usr/bin/env python3
"""Check every dashboard PromQL target, both directly and through Grafana."""
import argparse
import datetime
import json
import math
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
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
    panels = [panel for path in sorted((root / "dashboards").glob("*.json"))
              for panel in json.loads(path.read_text())["panels"]]
    datasources = [d for d in get(grafana + "/api/datasources")
                   if d["type"] == "prometheus"]
    if len(datasources) != 1:
        raise SystemExit("Expected one local Prometheus datasource")
    uid = datasources[0]["uid"]
    results = []
    for panel in panels:
        for target in panel.get("targets", []):
            expr = (target["expr"].replace("${instance:regex}", ".*")
                    .replace("$__rate_interval", "1m"))
            deadline = time.monotonic() + args.wait_seconds
            optional = panel["fieldConfig"]["defaults"].get("noValue") == "Not exercised / no samples"
            while True:
                query = urllib.parse.urlencode({"query": expr})
                direct = get(prometheus + "/api/v1/query?" + query)
                proxy = get(grafana + "/api/datasources/proxy/uid/" +
                            uid + "/api/v1/query?" + query)
                if direct.get("status") != "success" or proxy.get("status") != "success":
                    raise SystemExit(f"Query failed: {expr}")
                samples = proxy["data"]["result"]
                finite = samples and all(math.isfinite(float(s["value"][1]))
                                         for s in samples)
                if expr.startswith("up{"):
                    finite = finite and all(float(s["value"][1]) == 1 for s in samples)
                if optional or finite or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            status = "PASS" if finite else ("NOT EXERCISED" if optional else "FAIL")
            results.append({"panel": panel["title"], "query": expr,
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
              "skipper_sha": args.sha,
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
    output.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(1 if any(r["status"] == "FAIL" for r in results) else 0)
