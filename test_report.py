import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import lifecycle
import report

HERE = Path(__file__).resolve().parent
STATS_HEADER = ("Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,"
                "Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,"
                "50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%")
STATS_ROWS = [
    "GET,swr/shop_search,120,3,4,5.5,1,80,150,2.0,0.0,4,5,6,7,8,20,30,60,80,80",
    "GET,hard/site_meta,80,1,2,3.25,1,40,120,1.0,0.0,2,3,3,4,5,9,12,30,40,40",
    ",Aggregated,200,4,3,4.5,1,80,140,3.5,0.0,3,4,5,6,7,15,25,60,80,80",
]
FAILURES_HEADER = "Method,Name,Error,Occurrences"
EXCEPTIONS_HEADER = "Count,Message,Traceback,Nodes"
META = {"scenario": "smoke", "duration_seconds": 120, "max_vus": 60,
        "started": "2026-10-25T06:36:05+0100", "started_ms": 1792906565000,
        "ended": "2026-10-25T06:38:05+0100", "ended_ms": 1792906685000,
        "cache_e2e_commit": "4b19510", "skipper_sha": "abc1234"}


def make_run(root, files=None, meta=META):
    run = root / "20261025-063605-smoke"
    run.mkdir()
    if meta is not None:
        (run / "run.json").write_text(json.dumps(meta))
    for name, text in (files or {}).items():
        (run / name).write_text(text)
    return run


class Scratch(unittest.TestCase):
    """Fixtures live under .runtime/ (gitignored) rather than the system temp directory."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=HERE / ".runtime")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)


class Parsing(Scratch):
    def test_aggregated_row_drives_summary(self):
        run = make_run(self.root, {"locust_stats.csv": "\n".join([STATS_HEADER, *STATS_ROWS]) + "\n"})
        text = report.render(run)
        self.assertIn("| Requests | Failures | Failure % | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | RPS (avg) |", text)
        self.assertIn("| 200 | 4 | 2.00% | 4.5 | 3.0 | 15.0 | 60.0 | 3.50 |", text)

    def test_endpoints_sorted_by_request_count_and_exclude_aggregated(self):
        run = make_run(self.root, {"locust_stats.csv": "\n".join([STATS_HEADER, *STATS_ROWS]) + "\n"})
        endpoints = report.endpoints_section(report.read_rows(run / "locust_stats.csv"))
        self.assertIn("| GET | swr/shop_search | 120 | 3 | 2.50% | 5.5 | 4.0 | 20.0 | 60.0 | 2.00 |", endpoints)
        self.assertLess(endpoints.index("swr/shop_search"), endpoints.index("hard/site_meta"))
        self.assertNotIn("Aggregated", endpoints)

    def test_failures_and_exceptions_are_listed(self):
        run = make_run(self.root, {
            "locust_failures.csv": f"{FAILURES_HEADER}\nGET,swr/shop_search,HTTPError(503),3\n",
            "locust_exceptions.csv": f"{EXCEPTIONS_HEADER}\n2,boom|pipe,tb,node\n5,other,tb,node\n"})
        text = report.render(run)
        self.assertIn("| GET | swr/shop_search | HTTPError(503) | 3 |", text)
        self.assertIn("2 distinct, 7 total occurrences.", text)
        self.assertIn("| 2 | boom\\|pipe |", text)


class MissingFiles(Scratch):
    def test_missing_csvs_render_notes(self):
        text = report.render(make_run(self.root))
        self.assertIn("No `Aggregated` row in `locust_stats.csv`", text)
        self.assertIn("`locust_failures.csv` not found.", text)
        self.assertIn("`locust_exceptions.csv` not found.", text)

    def test_empty_csvs_render_none_recorded(self):
        run = make_run(self.root, {"locust_stats.csv": "", "locust_failures.csv": "",
                                   "locust_exceptions.csv": ""})
        text = report.render(run)
        self.assertIn("No `Aggregated` row", text)
        self.assertEqual(text.count("None recorded."), 2)

    def test_missing_run_json_still_renders(self):
        text = report.render(make_run(self.root, meta=None), grafana="http://g")
        self.assertIn("- **Scenario:** unknown", text)
        self.assertIn("Run start or end time not recorded", text)

    def test_zero_requests_has_no_failure_percentage(self):
        row = "GET,x,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0"
        run = make_run(self.root, {"locust_stats.csv": f"{STATS_HEADER}\n,Aggregated,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0\n{row}\n"})
        self.assertIn("| 0 | 0 | n/a |", report.render(run))


class Grafana(Scratch):
    def test_links_carry_run_time_range(self):
        links = dict(report.grafana_links("http://127.0.0.1:55853/", 1000, 2000))
        self.assertEqual(links["Cache Operations"],
                         "http://127.0.0.1:55853/d/cache-performance-local?orgId=1&from=1000&to=2000")
        self.assertEqual(links["Retail Application"],
                         "http://127.0.0.1:55853/d/retail-application-local?orgId=1&from=1000&to=2000")

    def test_render_uses_run_start_and_end_epoch_ms(self):
        text = report.render(make_run(self.root), grafana="http://127.0.0.1:55853")
        self.assertIn("from=1792906565000&to=1792906685000", text)
        self.assertIn("[Cache Operations](http://127.0.0.1:55853/d/cache-performance-local?", text)

    def test_base_parsed_from_urls_file(self):
        urls = "grafana: http://127.0.0.1:55853\nprometheus: http://127.0.0.1:55854\n"
        self.assertEqual(report.grafana_base(urls), "http://127.0.0.1:55853")
        self.assertIsNone(report.grafana_base("prometheus: http://127.0.0.1:55854\n"))

    def test_missing_base_skips_links_with_note(self):
        text = report.render(make_run(self.root), grafana=None)
        self.assertIn("Grafana links unavailable", text)
        self.assertNotIn("/d/cache-performance-local", text)

    def test_screenshots_embedded_when_present(self):
        run = make_run(self.root)
        shots = run / "screenshots"
        shots.mkdir()
        (shots / "cache-performance-cache-hit-ratio.png").write_bytes(b"png")
        text = report.render(run, grafana=None)
        self.assertIn("![cache-performance-cache-hit-ratio](screenshots/cache-performance-cache-hit-ratio.png)", text)

    def test_missing_screenshots_note(self):
        text = report.render(make_run(self.root), grafana=None)
        self.assertIn("No panel screenshots", text)


class Window(Scratch):
    def test_legacy_timed_run_uses_planned_end(self):
        meta = {"scenario": "smoke", "duration_seconds": 120, "started": "2026-10-25T06:36:05+0100"}
        text = report.render(make_run(self.root, meta=meta), grafana="http://g")
        self.assertIn("2m 00s (planned)", text)
        self.assertIn("from=1792906565000&to=1792906685000", text)

    def test_endless_run_without_end_has_no_range(self):
        meta = {"scenario": "endless", "duration_seconds": 0, "started_ms": 1792906565000}
        text = report.render(make_run(self.root, meta=meta), grafana="http://g")
        self.assertIn("- **Duration:** unknown", text)
        self.assertIn("end time not recorded", text)

    def test_duration_format(self):
        self.assertEqual(report.format_duration(125), "2m 05s")
        self.assertEqual(report.format_duration(3725), "1h 02m 05s")


class Table(unittest.TestCase):
    def test_markdown_table_layout_and_escaping(self):
        table = report.markdown_table(["A", "B"], [["x|y", 2]])
        self.assertEqual(table, "| A | B |\n|---|---|\n| x\\|y | 2 |")

    def test_number_handles_na(self):
        self.assertIsNone(report.number("N/A"))
        self.assertIsNone(report.number(""))
        self.assertEqual(report.number("2.5"), 2.5)


class FinishRun(Scratch):
    def test_stamps_end_and_writes_report(self):
        meta = dict(META)
        meta.pop("ended"), meta.pop("ended_ms")
        run = make_run(self.root, meta=meta)
        with contextlib.redirect_stdout(io.StringIO()):
            lifecycle.finish_run(run)
        stamped = json.loads((run / "run.json").read_text())
        self.assertIn("ended", stamped)
        self.assertIsInstance(stamped["ended_ms"], int)
        self.assertTrue((run / "report.md").exists())

    def test_report_failure_is_swallowed(self):
        missing = self.root / "does-not-exist"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            lifecycle.finish_run(missing)
        self.assertIn("report not written", err.getvalue())


if __name__ == "__main__":
    unittest.main()
