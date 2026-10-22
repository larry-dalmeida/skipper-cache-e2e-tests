import argparse
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import verify


class HarnessTests(unittest.TestCase):
    def run_harness(self, fetch=None, count=1):
        output = Path(".runtime/harness-unit-result.json")
        args = argparse.Namespace(label="unit", sha="unit", namespace="cache-e2e",
                                  base="http://unused", output=str(output))

        def counts(_namespace, _since, marker):
            return 3 if marker.endswith("-vary") else count

        def response(_base, path, vary=False):
            if vary:
                marker = path.split("run=")[1]
                return {"Vary": ["*"], "run": [marker]}
            return {"url": path}

        with patch.object(verify, "fetch", side_effect=fetch or response), \
                patch.object(verify, "origin_count", side_effect=counts), \
                patch.object(verify.time, "sleep"), \
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
                              return_value=Response(json.dumps({"url": url}).encode())):
                with self.assertRaisesRegex(AssertionError, "mismatch"):
                    verify.fetch("http://unused", "/get?run=expected")


if __name__ == "__main__":
    unittest.main()
