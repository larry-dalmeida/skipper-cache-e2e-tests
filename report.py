#!/usr/bin/env python3
"""Markdown report for one Locust run folder, built from its CSV and JSON files.

Kept free of Locust imports so it can be unit-tested without the dependency.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

DASHBOARDS = (("Cache Operations", "cache-performance-local"),
              ("Retail Application", "retail-application-local"))
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TOP_ENDPOINTS = 10
LIST_LIMIT = 20


def read_rows(path):
    """Rows of a Locust CSV; a missing or empty file yields no rows."""
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def cell(row, key):
    return row.get(key) or ""


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_meta(run):
    try:
        return json.loads((run / "run.json").read_text())
    except (OSError, ValueError):
        return {}


def parse_iso_ms(text):
    try:
        return int(datetime.strptime(text, TIMESTAMP_FORMAT).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def window(meta):
    """(start_ms, end_ms, end_is_planned). Runs without an end stamp fall back to start + planned duration."""
    start = meta.get("started_ms")
    if start is None:
        start = parse_iso_ms(meta.get("started"))
    end = meta.get("ended_ms")
    if end is None and start is not None and meta.get("duration_seconds"):
        return start, start + meta["duration_seconds"] * 1000, True
    return start, end, False


def format_time(ms, text):
    if text:
        return text
    if ms is None:
        return "not recorded"
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime(TIMESTAMP_FORMAT)


def format_duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


def markdown_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(lines)


def fmt_ms(value):
    return "n/a" if value is None else f"{value:.1f}"


def stats_cells(row):
    """Requests, failures, failure %, latency percentiles (ms) and average RPS for one stats row."""
    requests = number(row.get("Request Count")) or 0
    failures = number(row.get("Failure Count")) or 0
    failure_pct = f"{100 * failures / requests:.2f}%" if requests else "n/a"
    rps = number(row.get("Requests/s"))
    return [str(int(requests)), str(int(failures)), failure_pct,
            fmt_ms(number(row.get("Average Response Time"))),
            fmt_ms(number(row.get("50%"))), fmt_ms(number(row.get("95%"))),
            fmt_ms(number(row.get("99%"))), "n/a" if rps is None else f"{rps:.2f}"]


def summary_section(aggregated):
    if aggregated is None:
        return "No `Aggregated` row in `locust_stats.csv` (file missing or empty)."
    headers = ["Requests", "Failures", "Failure %", "Avg (ms)", "p50 (ms)", "p95 (ms)",
               "p99 (ms)", "RPS (avg)"]
    return markdown_table(headers, [stats_cells(aggregated)])


def endpoints_section(rows):
    endpoints = [r for r in rows if r.get("Name") != "Aggregated"]
    if not endpoints:
        return "No per-endpoint rows in `locust_stats.csv`."
    top = sorted(endpoints, key=lambda r: number(r.get("Request Count")) or 0, reverse=True)
    headers = ["Method", "Endpoint", "Requests", "Failures", "Failure %", "Avg (ms)",
               "p50 (ms)", "p95 (ms)", "p99 (ms)", "RPS (avg)"]
    rows = [[cell(r, "Type"), cell(r, "Name")] + stats_cells(r) for r in top[:TOP_ENDPOINTS]]
    text = markdown_table(headers, rows)
    if len(endpoints) > TOP_ENDPOINTS:
        text += f"\n\nTop {TOP_ENDPOINTS} of {len(endpoints)} endpoints by request count."
    return text


def failures_section(run):
    path = run / "locust_failures.csv"
    if not path.exists():
        return "`locust_failures.csv` not found."
    rows = read_rows(path)
    if not rows:
        return "None recorded."
    table = markdown_table(["Method", "Endpoint", "Error", "Occurrences"],
                           [[cell(r, "Method"), cell(r, "Name"), cell(r, "Error"),
                             cell(r, "Occurrences")] for r in rows[:LIST_LIMIT]])
    if len(rows) > LIST_LIMIT:
        table += f"\n\n{len(rows) - LIST_LIMIT} more rows not shown."
    return table


def exceptions_section(run):
    path = run / "locust_exceptions.csv"
    if not path.exists():
        return "`locust_exceptions.csv` not found."
    rows = read_rows(path)
    if not rows:
        return "None recorded."
    total = sum(number(r.get("Count")) or 0 for r in rows)
    table = markdown_table(["Count", "Message"],
                           [[cell(r, "Count"), cell(r, "Message")] for r in rows[:LIST_LIMIT]])
    text = f"{len(rows)} distinct, {int(total)} total occurrences.\n\n{table}"
    if len(rows) > LIST_LIMIT:
        text += f"\n\n{len(rows) - LIST_LIMIT} more rows not shown."
    return text


def faults_section(run):
    """Fault timeline from run/faults.json, written by chaos runs; empty when absent."""
    path = run / "faults.json"
    if not path.exists():
        return None
    try:
        timeline = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return f"`faults.json` unreadable: {error}"
    rows = [[format_time(f.get("actual_epoch_ms"), None), f.get("action"),
             "" if f.get("scheduled_offset") is None else f"{f['scheduled_offset']}s",
             f.get("detail") or ""] for f in timeline.get("faults", [])]
    if not rows:
        return "No faults recorded."
    return markdown_table(["Time", "Action", "Scheduled offset", "Detail"], rows)


def grafana_base(urls_text):
    """Grafana base URL from the `grafana: <url>` line of .runtime/urls.txt, if present."""
    for line in urls_text.splitlines():
        label, _, url = line.partition(":")
        if label.strip() == "grafana":
            return url.strip()
    return None


def grafana_links(base, start_ms, end_ms):
    query = f"orgId=1&from={start_ms}&to={end_ms}"
    return [(label, f"{base.rstrip('/')}/d/{uid}?{query}") for label, uid in DASHBOARDS]


def grafana_section(grafana, start, end):
    if grafana is None:
        return ("Grafana links unavailable: `.runtime/urls.txt` has no grafana URL. "
                "Run `python3 lifecycle.py urls` while the UI tunnel is up, then regenerate the report.")
    if start is None or end is None:
        return "Run start or end time not recorded; Grafana time range unavailable."
    links = [f"- [{label}]({url})" for label, url in grafana_links(grafana, start, end)]
    return ("\n".join(links) + f"\n\nTime range: `from={start}` to `to={end}` (epoch ms). "
            "The base URL is the one in `.runtime/urls.txt` at report time; ports change between runs.")


def screenshots_section(run):
    """Embedded panel screenshots from `run/screenshots/`, written by screenshots.py."""
    images = sorted((run / "screenshots").glob("*.png")) if (run / "screenshots").is_dir() else []
    if not images:
        return "No panel screenshots. Run `python3 screenshots.py <run folder>`."
    # Images are in the same directory as report.md, so use relative path
    return "\n\n".join(f"### {image.stem}\n\n![{image.stem}](./screenshots/{image.name})"
                       for image in images)


def render(run, grafana=None):
    """Markdown report for one run folder; `grafana` is the Grafana base URL, if known."""
    meta = load_meta(run)
    start, end, planned = window(meta)
    rows = read_rows(run / "locust_stats.csv")
    aggregated = next((r for r in rows if r.get("Name") == "Aggregated"), None)
    if start is not None and end is not None:
        duration = format_duration((end - start) / 1000) + (" (planned)" if planned else "")
    else:
        duration = "unknown"
    ended = format_time(end, meta.get("ended"))
    if planned:
        ended += " (planned)"
    max_vus = meta.get("max_vus")
    lines = [
        f"# Run report: {run.name}", "",
        f"- **Scenario:** {meta.get('scenario', 'unknown')}",
        f"- **Started:** {format_time(start, meta.get('started'))}",
        f"- **Ended:** {ended}",
        f"- **Duration:** {duration}",
        f"- **Max VUs:** {max_vus if max_vus is not None else 'not recorded'}",
        f"- **cache-e2e commit:** {meta.get('cache_e2e_commit', 'not recorded')}",
        f"- **Skipper SHA:** {meta.get('skipper_sha', 'not recorded')}",
        "", "## Summary", "", summary_section(aggregated),
        "", "## Endpoints", "", endpoints_section(rows),
        "", "## Failures", "", failures_section(run),
        "", "## Exceptions", "", exceptions_section(run),
        "", "## Grafana", "", grafana_section(grafana, start, end),
        "", "## Panel screenshots", "", screenshots_section(run),
    ]
    faults = faults_section(run)
    if faults is not None:
        lines += ["", "## Fault timeline", "", faults]
    return "\n".join(lines) + "\n"


def write_report(run, grafana=None):
    path = run / "report.md"
    path.write_text(render(run, grafana))
    return path
