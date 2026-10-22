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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="Exact running Skipper source SHA")
    parser.add_argument("--output", default=".runtime/metrics.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    dashboard = json.loads((root / "dashboard.json").read_text())
    datasources = [d for d in get("http://127.0.0.1:3000/api/datasources")
                   if d["type"] == "prometheus"]
    if len(datasources) != 1:
        raise SystemExit("Expected one local Prometheus datasource")
    uid = datasources[0]["uid"]
    results = []
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            expr = target["expr"]
            deadline = time.monotonic() + 45
            optional = "storage_error_total" in expr
            while True:
                query = urllib.parse.urlencode({"query": expr})
                direct = get("http://127.0.0.1:9090/api/v1/query?" + query)
                proxy = get("http://127.0.0.1:3000/api/datasources/proxy/uid/" +
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
    output.with_name("metrics-samples.txt").write_text(
        "\n".join(line for line in exposition.splitlines()
                  if line.startswith(wanted)) + "\n")
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "skipper_sha": args.sha,
              "units": "seconds", "panels": results}
    output.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(1 if any(r["status"] == "FAIL" for r in results) else 0)
