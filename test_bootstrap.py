import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=bootstrap.STATE)
        self.addCleanup(self.scratch.cleanup)
        self.state = Path(self.scratch.name)
        self.patches = [patch.object(bootstrap, "STATE", self.state),
                        patch.object(bootstrap.lifecycle, "STATE", self.state)]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def record(self):
        path = self.state / "origin.json"
        path.write_text(json.dumps({"supervisor": {"pid": 123, "identity": "expected"},
                                    "children": [{"pid": 124, "identity": "child"}]}))
        return path

    def test_live_supervisor_reused(self):
        self.record()
        with patch.object(bootstrap.lifecycle, "identity", side_effect=["expected", "child"]), \
                patch.object(bootstrap.lifecycle, "ready"), \
                patch.object(bootstrap.lifecycle, "apply_origin_cases"), \
                patch.object(bootstrap.subprocess, "Popen") as spawn:
            bootstrap.ensure_supervisor("origin")
        spawn.assert_not_called()

    def test_start_failure_reports_log_without_waiting(self):
        with patch.object(bootstrap.subprocess, "Popen") as spawn:
            spawn.return_value.pid = 123
            spawn.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "exited before readiness; see"):
                bootstrap.ensure_supervisor("origin")

    def test_dead_record_removed(self):
        path = self.record()
        with patch.object(bootstrap.lifecycle, "identity",
                          side_effect=subprocess.CalledProcessError(1, "ps")):
            self.assertFalse(bootstrap.alive("origin"))
        self.assertFalse(path.exists())

    def test_pid_reuse_refused(self):
        path = self.record()
        with patch.object(bootstrap.lifecycle, "identity", return_value="different"):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                bootstrap.alive("origin")
        self.assertTrue(path.exists())

    def test_orphan_refused(self):
        self.record()
        with patch.object(bootstrap.lifecycle, "identity",
                          side_effect=[subprocess.CalledProcessError(1, "ps"), "child"]):
            with self.assertRaisesRegex(RuntimeError, "Orphaned"):
                bootstrap.alive("origin")

    def test_new_sha_restarts_skipper_only(self):
        (self.state / "skipper.sha").write_text("old")
        (self.state / "urls.txt").write_text("urls")
        with patch.object(bootstrap, "ensure_supervisor") as ensure, \
                patch.object(bootstrap, "alive", return_value=True), \
                patch.object(bootstrap.subprocess, "check_output", return_value="new\n"), \
                patch.object(bootstrap.lifecycle, "teardown") as teardown:
            bootstrap.start(Path("checkout"))
        teardown.assert_called_once_with(("skipper",))
        self.assertEqual(ensure.call_args_list[-1], call("skipper", Path("checkout")))

    def test_matching_sha_does_not_restart(self):
        (self.state / "skipper.sha").write_text("same\n")
        (self.state / "urls.txt").write_text("urls")
        with patch.object(bootstrap, "ensure_supervisor"), \
                patch.object(bootstrap, "alive", return_value=True), \
                patch.object(bootstrap.subprocess, "check_output", return_value="same\n"), \
                patch.object(bootstrap.lifecycle, "teardown") as teardown:
            bootstrap.start(Path("checkout"))
        teardown.assert_not_called()

    def test_smoke_rejects_failures_or_missing_requests(self):
        for requests, failures in (("0", "0"), ("10", "1"), ("10", "0")):
            (self.state / "locust_stats.csv").write_text(
                f"Name,Request Count,Failure Count\nAggregated,{requests},{failures}\n")
            if failures == "0" and requests != "0":
                bootstrap.smoke_result(self.state)
            else:
                with self.assertRaisesRegex(RuntimeError, "Smoke failed"):
                    bootstrap.smoke_result(self.state)

    def test_missing_dependency_clear_error(self):
        with patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Missing prerequisite: python3"):
                bootstrap.prerequisites("checkout")

    def test_verification_runs_both_checks_and_reports_failure(self):
        (self.state / "skipper.sha").write_text("exact-sha\n")
        with patch.object(bootstrap.lifecycle, "run_locust", return_value=self.state), \
                patch.object(bootstrap, "smoke_result"), \
                patch.object(bootstrap.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "verify-metrics.py exited 1"):
                bootstrap.verify()
        self.assertEqual(run.call_count, 2)
        for args, _ in run.call_args_list:
            self.assertIn("exact-sha", args[0])
