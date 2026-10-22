#!/usr/bin/env python3
"""Generate a portable Grafana dashboard from the measured exporter contract."""
import json
from pathlib import Path

DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
panels = []
SCOPE = 'job="skipper-local",instance=~"${instance:regex}"'


def panel(title, expressions, unit="short", description="", stat=False,
          legend="{{result}} {{code_class}}", stacked=False):
    index = len(panels)
    panels.append({
        "id": index + 1, "title": title, "description": description,
        "type": "stat" if stat else "timeseries",
        "datasource": DATASOURCE,
        "gridPos": {"x": (index % 2) * 12, "y": (index // 2) * 8,
                    "w": 12, "h": 8},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {
            "drawStyle": "line", "lineWidth": 1,
            "fillOpacity": 40 if stacked else 0,
            "stacking": {"mode": "normal" if stacked else "none", "group": "A"},
        }}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [{"refId": chr(65 + n), "expr": expression,
                     "datasource": DATASOURCE, "legendFormat": legend,
                     "instant": stat, "range": not stat}
                    for n, expression in enumerate(expressions)],
    })


panel("Skipper scrape health", ['up{' + SCOPE + '}'],
      description="1 means scrape succeeded, not that coalescing is correct.", stat=True)
overall = ('sum by (result, code_class) (rate(skipper_cache_result_total{' +
           SCOPE + ',result=~"hit|miss"}[5m]))')
panel("Overall Cache Result by Status Code Class", [overall], "reqps",
      "Only overall hit/miss; excludes tier, stale and error counters. "
      "Missing miss series on PR4364 alone is an instrumentation gap, not zero.")
for tier in ("L1", "L2"):
    panel(tier + " Hits by Status Code Class", [
        'sum by (code_class) (rate(skipper_cache_result_total{' + SCOPE +
        ',result="' + tier.lower() + '_hit"}[5m]))'
    ], "reqps", "Tier diagnostics, not additional overall outcomes. "
       "Absent status classes are unexercised, not zero.", legend="{{code_class}}")
composition = ('sum by (result) (rate(skipper_cache_result_total{' +
               SCOPE + ',result=~"hit|miss"}[5m]))')
for stacked in (False, True):
    panel("Cache Hit vs Miss Rate" + (" (stacked)" if stacked else " (lines)"),
          [composition], "reqps",
          "Identical 5-minute rates and scope. Normal stacking retains request volume, "
          "not percentages. Tier hits are excluded; absent misses are not fabricated.",
          stacked=stacked, legend="{{result}}")
panel("L1 cache bytes by route", ["skipper_cache_lru_bytes{" + SCOPE + "}"], "bytes",
      legend="{{route}}")
panel("Duration observations (cumulative)", [
    'skipper_custom_duration_seconds_count{' + SCOPE +
    ',key=~"cache\\\\.(coalesce_wait|l2_get|l2_set)_duration"}'
], legend="{{key}}")
for label, key in (("Coalesce wait", "coalesce_wait"),
                   ("L2 get", "l2_get"), ("L2 set", "l2_set")):
    panel(label + " p95 (5 minute rate)", [
        'histogram_quantile(0.95, sum by (le) (rate('
        'skipper_custom_duration_seconds_bucket'
        '{' + SCOPE + ',key="cache.' + key + '_duration"}[5m])))'
    ], "s", "Exporter buckets and sums are seconds. Wait includes leader work; "
       "it is not a pure follower queue latency. No observations can yield NaN.")
panel("Storage faults (absent means NOT EXERCISED)", [
    "sum by (backend, op) (skipper_cache_storage_error_total{" + SCOPE + "})"
], description="Do not interpret absent series as a tested zero. Fault injection is "
               "covered by targeted Go tests, not by disrupting shared Valkey.",
   legend="{{backend}} {{op}}")

dashboard = {
    "uid": "ag1x69aba3qpse", "title": "Skipper Cache Verification",
    "description": "Local synthetic traffic only. Check results/ for exact source SHAs. "
                   "No nginx parity or production readiness claim.",
    "schemaVersion": 39, "version": 2, "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"}, "timezone": "utc",
    "tags": ["skipper", "cache", "local-verification"],
    "templating": {"list": [{
        "name": "datasource", "label": "Prometheus datasource",
        "type": "datasource", "query": "prometheus", "refresh": 1,
        "hide": 0, "multi": False, "includeAll": False, "options": [],
    }, {
        "name": "instance", "label": "Skipper instance", "type": "query",
        "datasource": DATASOURCE,
        "query": 'label_values(up{job="skipper-local"}, instance)',
        "refresh": 1, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"text": "All", "value": "$__all"}, "options": [],
    }]},
    "panels": panels,
}

if __name__ == "__main__":
    Path(__file__).with_name("dashboard.json").write_text(
        json.dumps(dashboard, indent=2) + "\n")
