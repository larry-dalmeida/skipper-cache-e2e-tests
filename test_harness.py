import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import verify

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("verify_metrics", HERE / "verify-metrics.py")
verify_metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_metrics)


class HarnessTests(unittest.TestCase):
    def run_harness(self, fetch=None, count=1):
        output = Path(".runtime/harness-unit-result.json")
        args = argparse.Namespace(label="unit", sha="unit", control="http://control",
                                  base="http://unused", output=str(output))

        def counts(_control, marker):
            return 3 if marker.endswith("-vary") else count

        def response(_base, path, vary=False):
            return {"path": path.split("?")[0], "query": path.split("?")[1]}

        with patch.object(verify, "configure"), \
                patch.object(verify, "fetch", side_effect=fetch or response), \
                patch.object(verify, "origin_count", side_effect=counts), \
                contextlib.redirect_stdout(io.StringIO()):
            code = verify.verify(args)
        report = json.loads(output.read_text())
        output.unlink()
        return code, report

    def test_success(self):
        self.assertEqual(self.run_harness()[0], 0)

    def test_origin_amplification_fails(self):
        code, report = self.run_harness(count=10)
        self.assertEqual(code, 1)
        self.assertEqual(report["cases"][1]["status"], "FAIL")
        self.assertIn("origin cold=10", report["cases"][1]["error"])

    def test_request_failure_is_not_discarded(self):
        def fail(*_args, **_kwargs):
            raise RuntimeError("HTTP 500 / failed request")

        code, report = self.run_harness(fetch=fail)
        self.assertEqual(code, 1)
        self.assertTrue(all(case["status"] == "FAIL" for case in report["cases"]))

    def test_bad_status_rejected(self):
        class Response:
            status = 500

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        with patch.object(verify.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(AssertionError, "HTTP 500"):
                verify.fetch("http://unused", "/get")

    def test_wrong_path_or_query_rejected(self):
        class Response(io.BytesIO):
            status = 200

        for url in ("/wrong?run=expected", "/get?run=wrong"):
            with patch.object(verify.urllib.request, "urlopen",
                              return_value=Response(json.dumps({"path": url.split("?")[0], "query": url.split("?")[1]}).encode())):
                with self.assertRaisesRegex(AssertionError, "mismatch"):
                    verify.fetch("http://unused", "/get?run=expected")


class VerifyMetricsUrls(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=HERE / ".runtime")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_stack_urls_come_from_runtime_urls_file(self):
        (self.root / ".runtime").mkdir()
        (self.root / ".runtime" / "urls.txt").write_text(
            "grafana: http://127.0.0.1:55853/\nprometheus: http://127.0.0.1:55854\n"
            "cache-operations: http://127.0.0.1:55853/d/cache-performance-local/cache-operations\n")
        self.assertEqual(verify_metrics.stack_urls(self.root),
                         {"grafana": "http://127.0.0.1:55853", "prometheus": "http://127.0.0.1:55854"})

    def test_invalid_retry_window_rejected_before_network_access(self):
        for window in ("-1", "nan", "inf"):
            result = subprocess.run([sys.executable, str(HERE / "verify-metrics.py"),
                                     "--sha", "unit", "--wait-seconds", window],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be finite and nonnegative", result.stderr)

    def test_stack_urls_default_without_urls_file(self):
        self.assertEqual(verify_metrics.stack_urls(self.root),
                         {"grafana": "http://127.0.0.1:3000", "prometheus": "http://127.0.0.1:9090"})


class VerifyMetricQueries(unittest.TestCase):
    def samples(self, *values):
        return [{"metric": {"route": str(i)}, "value": [0, str(value)]}
                for i, value in enumerate(values)]

    def test_dashboard_all_values_and_builtins(self):
        dashboard = {"time": {"from": "now-30m"}, "templating": {"list": [
            {"name": "origin", "includeAll": True, "allValue": ".+",
             "current": {"value": ["$__all"]}},
            {"name": "family", "current": {"value": ["products", "categories"]}},
            {"name": "locale", "current": {"value": "de_DE"}},
        ]}}
        result = verify_metrics.expand_query(
            '$origin ${family:regex} $locale $__rate_interval $__interval $__range $__range_s',
            verify_metrics.dashboard_variables(dashboard))
        self.assertEqual(result, '.+ products|categories de_DE 1m 1m 30m 1800')

    def test_custom_all_without_explicit_all_value(self):
        values = verify_metrics.dashboard_variables({"templating": {"list": [
            {"name": "family", "type": "custom", "query": "shop,contentful",
             "includeAll": True, "current": {"value": "$__all"}},
        ]}})
        self.assertEqual(verify_metrics.expand_query('${family:regex}', values), 'shop|contentful')

    def test_unresolved_variables_fail_instead_of_matching_nothing(self):
        with self.assertRaisesRegex(ValueError, 'Unresolved dashboard variable: missing'):
            verify_metrics.expand_query('${missing:regex}', {})
        with self.assertRaisesRegex(ValueError, 'No default/All value'):
            verify_metrics.dashboard_variables({"templating": {"list": [
                {"name": "origin", "type": "query"}]}})

    def test_promql_capture_group_is_not_a_variable(self):
        self.assertEqual(verify_metrics.expand_query('label_replace(m,"family","$1","route","x")', {}),
                         'label_replace(m,"family","$1","route","x")')

    def test_explicit_regex_values_escaped_for_promql(self):
        self.assertEqual(verify_metrics.expand_query('${origin:regex}', {"origin": ["a.b", "c"]}),
                         r'a\\.b|c')

    def test_every_dashboard_query_expands_without_leftover_variables(self):
        for path in (HERE / 'dashboards').glob('*.json'):
            dashboard = json.loads(path.read_text())
            values = verify_metrics.dashboard_variables(dashboard)
            for panel in dashboard['panels']:
                for target in panel.get('targets', []):
                    expression = verify_metrics.expand_query(target['expr'], values)
                    self.assertIsNone(verify_metrics.VARIABLE.search(expression), expression)

    def test_histogram_accepts_active_series_among_inactive_nan(self):
        self.assertEqual(verify_metrics.sample_status('histogram_quantile(0.95, m)',
                                                     self.samples('NaN', 0.25)), 'PASS')
        for values in (('NaN',), ('NaN', 'NaN'), ('+Inf', 0.25)):
            self.assertEqual(verify_metrics.sample_status('histogram_quantile(0.95, m)',
                                                         self.samples(*values)), 'FAIL')

    def test_non_histogram_nonfinite_and_down_scrapes_remain_failures(self):
        self.assertEqual(verify_metrics.sample_status('sum(m)', self.samples('NaN', 1)), 'FAIL')
        self.assertEqual(verify_metrics.sample_status('up{job="skipper-local"}', self.samples(1, 0)), 'FAIL')
        self.assertEqual(verify_metrics.sample_status('up{job="skipper-local"}', self.samples(1)), 'PASS')

    def test_direct_and_grafana_must_both_pass(self):
        success = {"status": "success", "data": {"result": self.samples(1)}}
        absent = {"status": "success", "data": {"result": []}}
        self.assertEqual(verify_metrics.query_status('sum(m)', success, success)[0], 'PASS')
        self.assertEqual(verify_metrics.query_status('sum(m)', absent, success)[0], 'FAIL')
        self.assertEqual(verify_metrics.query_status('sum(m)', success, absent)[0], 'FAIL')
        for direct, proxy in (({"status": "error"}, success), (success, {"status": "error"})):
            with self.assertRaisesRegex(ValueError, 'Query failed'):
                verify_metrics.query_status('sum(m)', direct, proxy, allow_missing=True)

    def test_missing_event_allowlist_is_scenario_specific(self):
        refresh, evictions = ('cache-performance-local', 19), ('cache-performance-local', 60)
        self.assertIn(refresh, verify_metrics.UNEXERCISED_EVENTS['smoke'])
        self.assertIn(evictions, verify_metrics.UNEXERCISED_EVENTS['smoke'])
        self.assertNotIn(refresh, verify_metrics.UNEXERCISED_EVENTS['origin-stale'])
        self.assertFalse(verify_metrics.UNEXERCISED_EVENTS['strict'])
        self.assertEqual(verify_metrics.sample_status('sum(m)', [], True), 'NOT EXERCISED')
        self.assertEqual(verify_metrics.sample_status('sum(m)', [], False), 'FAIL')
        self.assertEqual(verify_metrics.sample_status('sum(m)', self.samples('NaN'), True), 'FAIL')

    def test_route_cache_panels_use_route_metrics_and_derive_display_family(self):
        dashboard = json.loads((HERE / 'dashboards/cache-performance.json').read_text())
        panels = {panel['id']: panel for panel in dashboard['panels']}
        expected = {52: 'skipper_cache_route_result_total',
                    53: 'skipper_cache_route_reval_total',
                    54: 'skipper_cache_origin_duration_seconds_bucket'}
        for panel_id, metric in expected.items():
            expression = verify_metrics.expand_query(panels[panel_id]['targets'][0]['expr'],
                                                     verify_metrics.dashboard_variables(dashboard))
            self.assertIn(metric, expression)
            self.assertIn('label_replace(', expression)
            self.assertIn('"route", "(.+?)__.*"', expression)
            self.assertNotIn('family!=', expression)

    def test_route_refresh_outcomes_include_failed_samples(self):
        dashboard = json.loads((HERE / 'dashboards/cache-performance.json').read_text())
        panel = next(panel for panel in dashboard['panels'] if panel['id'] == 53)
        expression = verify_metrics.expand_query(panel['targets'][0]['expr'],
                                                 verify_metrics.dashboard_variables(dashboard))
        self.assertIn('sum by (family, outcome)', expression)
        self.assertNotIn('outcome=', expression)
        failed = [{"metric": {"route": "contentful_spaces__all", "outcome": "failed"},
                   "value": [0, "3"]}]
        self.assertEqual(verify_metrics.query_status(expression,
            {"status": "success", "data": {"result": failed}},
            {"status": "success", "data": {"result": failed}})[0], 'PASS')

    def test_error_panels_zero_only_from_real_observed_traffic(self):
        dashboard = json.loads((HERE / 'dashboards/retail-application.json').read_text())
        for panel in dashboard['panels']:
            if panel['id'] in (7, 11):
                expression = panel['targets'][0]['expr']
                self.assertIn('or on (', expression)
                self.assertIn('(0 * sum by', expression)
                self.assertNotIn('vector(0)', expression)


class OriginControlTests(unittest.TestCase):
    def test_configuration_sends_real_delay_and_cache_lifetime(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(verify.urllib.request, "urlopen", return_value=response) as fetch:
            verify.configure("http://control", "unique-case", delay=1, vary="*")
        payload = json.loads(fetch.call_args[0][0].data)
        self.assertEqual(payload, {"case": "unique-case", "delay": 1, "vary": "*",
                                   "cache_control": "public, max-age=30"})

    def test_counter_uses_filtered_control_events(self):
        response = io.BytesIO(b'{"counts":{"total":3}}')
        with patch.object(verify.urllib.request, "urlopen", return_value=response) as fetch:
            self.assertEqual(verify.origin_count("http://control", "unique-case"), 3)
        self.assertEqual(fetch.call_args[0][0], "http://control/events?case=unique-case")


if __name__ == "__main__":
    unittest.main()
