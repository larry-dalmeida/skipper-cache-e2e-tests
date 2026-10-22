#!/usr/bin/env python3
"""Generate a portable Grafana dashboard from the measured exporter contract."""
import json
from pathlib import Path

DATASOURCE = {"type": "prometheus", "uid": "${datasource}"}
panels = []
SCOPE = 'job="skipper-local",instance=~"${instance:regex}"'
slot = 0
base_y = 0
WINDOW = "$__rate_interval"


def outcome_rate(keys, grouped=False):
    return ('sum' + (' by (key)' if grouped else '') +
            ' (rate(skipper_custom_total{' + SCOPE +
            ',key=~"cache\\\\.(' + keys + ')"}[' + WINDOW + ']))')


denominator = outcome_rate("hit|miss|stale")
fresh_hits = outcome_rate("hit")
scrape_ok = '(min(up{' + SCOPE + '}) == 1)'
fresh_ratio = ('(100 * ((' + fresh_hits + ') or (0 * (' + denominator +
               '))) / ((' + denominator + ') > 0)) and on() ' + scrape_ok)


def panel(title, expressions, unit="short", description="", stat=False,
          legend="{{result}} {{code_class}}", stacked=False, optional=False):
    global slot
    index = len(panels)
    panels.append({
        "id": index + 1, "title": title, "description": description,
        "type": "stat" if stat else "timeseries",
        "datasource": DATASOURCE,
        "gridPos": {"x": (slot % 2) * 12, "y": base_y + (slot // 2) * 8,
                    "w": 12, "h": 8},
        "fieldConfig": {"defaults": {"unit": unit,
            "noValue": "Not exercised / no samples" if optional else "No traffic / no data",
            "custom": {
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
    slot += 1


def row(title):
    global slot, base_y
    base_y += ((slot + 1) // 2) * 8
    panels.append({"id": len(panels) + 1, "type": "row", "title": title,
                   "collapsed": False, "panels": [],
                   "gridPos": {"x": 0, "y": base_y, "w": 24, "h": 1}})
    base_y += 1
    slot = 0


panel("Skipper scrape health", ['up{' + SCOPE + '}'],
      description="1 means scrape succeeded, not that coalescing is correct.", stat=True)
panel("Fresh Cache Hit % (hit / hit + miss + stale)", [fresh_ratio], "percent",
      "Flat request-outcome counters include fallback misses missing from PR4364's "
      "labeled schema. Excludes errors and tier hits. Zero denominator or a failed "
      "scrape yields no data, never 0%/100%; missing hits are zero-filled only "
      "against a present positive request denominator.")
overall = ('sum by (result, code_class) (rate(skipper_cache_result_total{' +
           SCOPE + ',result=~"hit|miss"}[' + WINDOW + ']))')
panel("Overall Cache Result by Status Code Class", [overall], "reqps",
      "Only overall hit/miss; excludes tier, stale and error counters. "
      "Missing miss series on PR4364 alone is an instrumentation gap, not zero.")
for tier in ("L1", "L2"):
    panel(tier + " Hits by Status Code Class", [
        'sum by (code_class) (rate(skipper_cache_result_total{' + SCOPE +
        ',result="' + tier.lower() + '_hit"}[' + WINDOW + ']))'
    ], "reqps", "Tier diagnostics, not additional overall outcomes. "
       "Absent status classes are unexercised, not zero.", legend="{{code_class}}")
composition = outcome_rate("hit|miss", grouped=True)
for stacked in (False, True):
    panel("Cache Hit vs Miss Rate" + (" (stacked)" if stacked else " (lines)"),
          [composition], "reqps",
          "Identical adaptive rate window and scope. Flat cache.hit/cache.miss cover "
          "fallback misses; normal stacking retains volume, not percentages. "
          "Errors and tier hits are excluded.", stacked=stacked, legend="{{key}}")
panel("Stale Cache Responses (separate from fresh hits)", [
    outcome_rate("stale", grouped=True)], "reqps",
    "Stale responses are in the fresh-hit denominator, never its numerator. "
    "Absent lazy counter means not exercised, not a verified zero.",
    legend="{{key}}", optional=True)
panel("L1 cache bytes by route", ["skipper_cache_lru_bytes{" + SCOPE + "}"], "bytes",
      legend="{{route}}")
panel("Duration observations (cumulative)", [
    'skipper_custom_duration_seconds_count{' + SCOPE +
    ',key=~"cache\\\\.(coalesce_wait|l2_get|l2_set)_duration"}'
], legend="{{key}}")
for label, key in (("Coalesce wait", "coalesce_wait"),
                   ("L2 get", "l2_get"), ("L2 set", "l2_set")):
    panel(label + " p95 (adaptive rate window)", [
        'histogram_quantile(0.95, sum by (le) (rate('
        'skipper_custom_duration_seconds_bucket'
        '{' + SCOPE + ',key="cache.' + key + '_duration"}[' + WINDOW + '])))'
    ], "s", "Exporter buckets and sums are seconds. Wait includes leader work; "
       "it is not a pure follower queue latency. No observations can yield NaN.")
row("Failure / fallback signals — absent lazy counters are NOT healthy zeros")
panel("L2 errors and fallbacks", [outcome_rate("l2_get_error|l2_set_fallback", True)],
      "ops", "Error/fallback events per second, not request outcomes. Absent means "
      "not exercised. Check scrape health independently.", legend="{{key}}", optional=True)
panel("Storage faults by backend / operation", [
    "sum by (backend, op) (rate(skipper_cache_storage_error_total{" + SCOPE +
    "}[" + WINDOW + "]))"
], "ops", "Faults per second; absent means not exercised, not zero.",
      legend="{{backend}} {{op}}", optional=True)
panel("Coalesce errors (per waiter, NOT origin failures)", [
    outcome_rate("coalesce_error|coalesce_bypass_error", True)], "ops",
    "Singleflight errors can be counted per waiter and fall back to normal proxying. "
    "NOT origin-failure rate or client HTTP 5xx rate.", legend="{{key}}", optional=True)
panel("Revalidation errors / dropped jobs", [
    outcome_rate("reval_error|reval_dropped", True)], "ops",
    "Background job events, not client outcomes. Lazy counters absent until exercised.",
    legend="{{key}}", optional=True)
panel("LRU evictions / oversized entries", [
    outcome_rate("lru_eviction|lru_oversized", True)], "ops",
    "Capacity events per second. Oversized entries are not stored. "
    "Absent counter is not a tested zero.", legend="{{key}}", optional=True)
panel("Revalidation queue depth", [
    'sum(skipper_custom_gauges{' + SCOPE + ',key="cache.reval_queue_depth"})'
], "short", "Current queued jobs, not a rate. A sampled 0 is an observed empty queue; "
   "an absent sample is unknown.", legend="queued jobs")

dashboard = {
    "uid": "ag1x69aba3qpse", "title": "Skipper Cache Verification",
    "description": "Local synthetic traffic only. Check results/ for exact source SHAs. "
                   "No nginx parity or production readiness claim.",
    "schemaVersion": 39, "version": 3, "refresh": "5s",
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
