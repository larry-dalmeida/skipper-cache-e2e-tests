#!/usr/bin/env python3
"""Observe L1 then L2 hits for one unique key; requires launcher L1 TTL=2s."""
import argparse
import datetime
import json
import re
import time
import urllib.request
import uuid
from pathlib import Path

from verify import fetch, origin_count, require


def counters():
    with urllib.request.urlopen("http://127.0.0.1:9911/metrics", timeout=5) as response:
        text = response.read().decode()
    values = {}
    for labels, value in re.findall(r"^skipper_cache_result_total\{([^}]+)\} (\S+)$",
                                    text, re.MULTILINE):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', labels))
        values[(labels["result"], labels["code_class"])] = float(value)
    return values


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", default=".runtime/tiers.json")
    args = parser.parse_args()
    since = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    marker = uuid.uuid4().hex + "-tiers"
    path = "/get?run=" + marker + "&check=tier"
    before = counters()
    cold = fetch("http://127.0.0.1:9999", path)
    warm = fetch("http://127.0.0.1:9999", path)
    l1 = counters()
    require(warm == cold, "warm body changed")
    require(l1.get(("l1_hit", "2xx"), 0) > before.get(("l1_hit", "2xx"), 0),
            "warm request did not emit l1_hit/2xx")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        require(fetch("http://127.0.0.1:9999", path) == cold, "tier body changed")
        after = counters()
        if after.get(("l2_hit", "2xx"), 0) > l1.get(("l2_hit", "2xx"), 0):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("no l2_hit/2xx within bounded L1 expiry window")
    calls = origin_count("cache-e2e", since, marker)
    require(calls == 1, f"origin calls={calls}; expected 1 including L2 fetch")
    report = {
        "utc": since, "skipper_sha": args.sha, "run_id": marker,
        "status": "PASS", "origin_calls": calls, "http_status": 200,
        "body_path_query": "matched", "l1_ttl_seconds": 2,
        "observed_labels": [
            {"result": result, "code_class": code, "count": value}
            for (result, code), value in sorted(after.items())],
        "verified": ["hit/2xx", "l1_hit/2xx", "l2_hit/2xx"],
        "not_exercised": ["miss (absent on exact PR4364)",
                          "tier hit code classes 4xx/5xx/other"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
