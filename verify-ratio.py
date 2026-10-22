#!/usr/bin/env python3
"""Evaluate the exact ratio algebra on isolated PromQL vectors, not live faults."""
import datetime
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request

from dashboard import denominator, fresh_hits, fresh_ratio, scrape_ok

EMPTY = "(vector(0) > 1)"
CASES = [
    ("fresh 2, miss 1, stale 1", 2, 4, True, 50),
    ("misses only, lazy hit counter absent", None, 3, True, 0),
    ("stale only, lazy hit counter absent", None, 2, True, 0),
    ("hits only", 3, 3, True, 100),
    ("zero requests", 0, 0, True, None),
    ("all outcome counters absent", None, None, True, None),
    ("scrape down despite previous data", 2, 3, False, None),
    ("scrape absent despite previous data", 2, 3, None, None),
]


def vector(value):
    return EMPTY if value is None else f"vector({value})"


if __name__ == "__main__":
    results = []
    for name, hits, total, up, expected in CASES:
        query = fresh_ratio.replace(denominator, vector(total)).replace(
            fresh_hits, vector(hits)).replace(
                scrape_ok, EMPTY if up is None else f"(vector({int(up)}) == 1)")
        url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=10) as response:
            result = json.load(response)
        assert result["status"] == "success", result
        samples = result["data"]["result"]
        if expected is None:
            assert not samples, (name, samples)
            observed = "NO DATA"
        else:
            observed = float(samples[0]["value"][1])
            assert math.isclose(observed, expected), (name, observed)
        results.append({"case": name, "status": "PASS", "observed": observed})
        print(f"PASS: {name}: {observed}")
    output = Path(".runtime/ratio-semantics.json")
    output.write_text(json.dumps({
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "method": "Real Prometheus evaluates exact dashboard ratio algebra with synthetic "
                  "vectors replacing input rates only; not live fault injection.",
        "exact_dashboard_query": fresh_ratio, "cases": results,
    }, indent=2) + "\n")
