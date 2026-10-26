import argparse
import contextlib
import importlib.util
import io
import json
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

    def test_stack_urls_default_without_urls_file(self):
        self.assertEqual(verify_metrics.stack_urls(self.root),
                         {"grafana": "http://127.0.0.1:3000", "prometheus": "http://127.0.0.1:9090"})


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
