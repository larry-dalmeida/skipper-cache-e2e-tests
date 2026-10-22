#!/usr/bin/env python3
"""Generate a portable Grafana dashboard from the measured exporter contract."""
import json
from pathlib import Path

DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
panels = []


def panel(title, expressions, unit="short", description="", stat=False):
    index = len(panels)
    panels.append({
        "id": index + 1, "title": title, "description": description,
        "type": "stat" if stat else "timeseries",
        "datasource": DATASOURCE,
        "gridPos": {"x": (index % 2) * 12, "y": (index // 2) * 8,
                    "w": 12, "h": 8},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [{"refId": chr(65 + n), "expr": expression,
                     "datasource": DATASOURCE, "legendFormat": "{{result}}{{key}}{{backend}} {{op}}",
                     "instant": stat, "range": not stat}
                    for n, expression in enumerate(expressions)],
    })


panel("Skipper scrape health", ['up{job="skipper-local"}'],
      description="1 means scrape succeeded, not that coalescing is correct.", stat=True)
panel("Cache outcomes (cumulative since process start)", ["skipper_cache_result_total"],
      description="PR4364 alone still has the RequestURI bug: coalesce_error is expected. "
                  "PR4365 fixes it but does not include PR4364's exporter additions.")
panel("L1 cache bytes by route", ["skipper_cache_lru_bytes"], "bytes")
panel("Duration observations (cumulative)", [
    'skipper_custom_duration_seconds_count{key=~"cache\\\\.(coalesce_wait|l2_get|l2_set)_duration"}'
])
for label, key in (("Coalesce wait", "coalesce_wait"),
                   ("L2 get", "l2_get"), ("L2 set", "l2_set")):
    panel(label + " p95 (5 minute rate)", [
        'histogram_quantile(0.95, sum by (le) (rate('
        'skipper_custom_duration_seconds_bucket'
        '{key="cache.' + key + '_duration"}[5m])))'
    ], "s", "Exporter buckets and sums are seconds. Wait includes leader work; "
       "it is not a pure follower queue latency. No observations can yield NaN.")
panel("Storage faults (absent means NOT EXERCISED)", [
    "sum by (backend, op) (skipper_cache_storage_error_total)"
], description="Do not interpret absent series as a tested zero. Fault injection is "
               "covered by targeted Go tests, not by disrupting shared Valkey.")

dashboard = {
    "uid": "ag1x69aba3qpse", "title": "Skipper Cache Verification",
    "description": "Local synthetic traffic only. Check results/ for exact source SHAs. "
                   "No nginx parity or production readiness claim.",
    "schemaVersion": 39, "version": 1, "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"}, "timezone": "utc",
    "tags": ["skipper", "cache", "local-verification"],
    "templating": {"list": [{
        "name": "datasource", "label": "Prometheus datasource",
        "type": "datasource", "query": "prometheus", "refresh": 1,
        "hide": 0, "multi": False, "includeAll": False, "options": [],
    }]},
    "panels": panels,
}

if __name__ == "__main__":
    Path(__file__).with_name("dashboard.json").write_text(
        json.dumps(dashboard, indent=2) + "\n")
