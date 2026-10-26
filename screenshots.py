#!/usr/bin/env python3
"""Capture Grafana panel screenshots for one run folder over its time window.

Uses Playwright's Chromium against Grafana's anonymous-viewer `d-solo` pages,
so no image renderer plugin is needed. Requires `playwright` in the harness venv.
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import urlencode

from report import grafana_base, load_meta, window

SCREENSHOT_DIR = "screenshots"
VIEWPORT = {"width": 1600, "height": 900}
RENDER_WAIT_MS = 4000

# (dashboard uid, panel id, file slug, panel title)
PANELS = (
    ("cache-performance-local", 45, "cache-hit-ratio", "Cache hit ratio"),
    ("cache-performance-local", 12, "hit-miss-stale-rate", "Hit / miss / stale rate"),
    ("cache-performance-local", 14, "l1-l2-hit-rate", "L1 / L2 hit rate"),
    ("cache-performance-local", 50, "origin-offload", "Origin offload % over time"),
    ("cache-performance-local", 16, "origin-responses-status", "Origin responses by status (all callers)"),
    ("cache-performance-local", 18, "origin-latency-p95", "Origin latency p95 (all callers)"),
    ("cache-performance-local", 19, "refresh-errors", "Refresh errors / dropped jobs"),
    ("cache-performance-local", 7, "request-rate", "Request rate"),
    ("cache-performance-local", 61, "time-to-first-hit", "Time to First Hit"),
    ("cache-performance-local", 58, "cache-entries", "Cache entries"),
    ("cache-performance-local", 59, "cache-bytes-vs-capacity", "Cache bytes vs capacity"),
    ("cache-performance-local", 52, "cache-outcomes-by-family", "Cache outcomes by family"),
    ("cache-performance-local", 53, "refresh-outcomes-by-family", "Refresh outcomes by family"),
    ("cache-performance-local", 54, "origin-latency-p95-by-family", "Origin latency p95 by family"),
    ("cache-performance-local", 57, "cache-control-directives", "Cache-Control directives"),
    ("retail-application-local", 5, "retail-origin-responses", "Origin responses by origin and status"),
    ("retail-application-local", 7, "retail-non-2xx", "Non-2xx responses by origin"),
)


def panel_url(base, uid, panel_id, start, end):
    """Full-screen view of one panel (`viewPanel`), with the Grafana chrome hidden (`kiosk`)."""
    query = urlencode({"orgId": 1, "viewPanel": f"panel-{panel_id}", "from": start, "to": end,
                       "kiosk": 1, "theme": "light", "refresh": ""})
    return f"{base.rstrip('/')}/d/{uid}/dashboard?{query}"


def screenshot_name(uid, slug):
    return f"{uid.replace('-local', '')}-{slug}.png"


def capture(run, grafana, executable_path=None):
    """Write one PNG per panel into `run/screenshots/`; returns the written paths."""
    from playwright.sync_api import sync_playwright

    start, end, _ = window(load_meta(run))
    if start is None or end is None:
        raise ValueError(f"{run.name}: run start/end not recorded; cannot set Grafana time range")
    out = run / SCREENSHOT_DIR
    out.mkdir(exist_ok=True)
    written = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable_path)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        for uid, panel_id, slug, _title in PANELS:
            page.goto(panel_url(grafana, uid, panel_id, start, end), wait_until="load", timeout=90000)
            page.wait_for_timeout(RENDER_WAIT_MS)
            path = out / screenshot_name(uid, slug)
            page.screenshot(path=str(path))
            written.append(path)
        browser.close()
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="run folder, e.g. results/runs/<run>")
    parser.add_argument("--grafana", help="Grafana base URL (default: grafana line in .runtime/urls.txt)")
    parser.add_argument("--chromium", help="path to a Chromium binary (default: Playwright's own)")
    args = parser.parse_args(argv)
    grafana = args.grafana
    if grafana is None:
        urls = Path(__file__).parent / ".runtime" / "urls.txt"
        grafana = grafana_base(urls.read_text()) if urls.exists() else None
    if not grafana:
        print("No Grafana URL: pass --grafana or run `python3 lifecycle.py urls`.", file=sys.stderr)
        return 1
    for path in capture(args.run, grafana, args.chromium):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
