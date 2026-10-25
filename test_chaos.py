import json
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import chaos
import report


class ScheduleTest(unittest.TestCase):
    def test_base_duration_uses_declared_offsets(self):
        self.assertEqual(chaos.schedule("origin-stale"),
                         [(60, "origin-503", "origin default case 503"),
                          (150, "origin-200", "origin default case restored to 200")])
        self.assertEqual([s[0] for s in chaos.schedule("valkey-kill")], [90, 150])
        self.assertEqual([s[1] for s in chaos.schedule("skipper-kill")], ["skipper-kill"])

    def test_offsets_scale_with_duration(self):
        self.assertEqual([s[0] for s in chaos.schedule("origin-stale", 90)], [18, 45])

    def test_rejects_unknown_scenario_and_bad_duration(self):
        with self.assertRaises(ValueError):
            chaos.schedule("nope")
        with self.assertRaises(ValueError):
            chaos.schedule("origin-stale", 0)

    def test_rejects_fault_past_duration(self):
        scenarios = {"late": {"description": "", "steps": [(400, "origin-503", "late")]}}
        with unittest.mock.patch.dict(chaos.SCENARIOS, scenarios):
            with self.assertRaises(ValueError):
                chaos.schedule("late", 300)

    def test_every_scenario_action_has_a_kind(self):
        for scenario in chaos.SCENARIOS.values():
            for _, action, _ in scenario["steps"]:
                self.assertIn(action, chaos.ACTION_KINDS)


class RunScheduleTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.run = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def fake_actions(self, fail=None):
        calls = []

        def make(name):
            def action():
                calls.append(name)
                if name == fail:
                    raise RuntimeError("boom")
                return f"{name} done"
            return action

        names = ["origin-503", "origin-200", "valkey-down", "valkey-up", "skipper-kill", "restore"]
        return {n: make(n) for n in names}, calls

    def test_writes_faults_json_with_actions_and_restore(self):
        actions, calls = self.fake_actions()
        annotations = []
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        steps = [(0, "origin-503", "down"), (0, "origin-200", "up")]
        timeline = chaos.run_schedule("origin-stale", 90, steps, self.run, stop, "http://g", actions,
                                      annotator=lambda g, s, text, ms: annotations.append((text, ms)) or True)
        saved = json.loads((self.run / "faults.json").read_text())
        self.assertEqual(saved, timeline)
        self.assertEqual(saved["scenario"], "origin-stale")
        self.assertEqual(saved["duration_seconds"], 90)
        self.assertIsNotNone(saved["run_started_ms"])
        self.assertIsNotNone(saved["run_ended_ms"])
        self.assertEqual([f["action"] for f in saved["faults"]], ["origin-503", "origin-200", "restore"])
        self.assertEqual(calls.count("restore"), 1)
        first = saved["faults"][0]
        self.assertEqual(first["scheduled_offset"], 0)
        self.assertIsInstance(first["actual_epoch_ms"], int)
        self.assertEqual(first["detail"], "origin-503 done")
        self.assertEqual([text for text, _ in annotations],
                         ["chaos start: down", "chaos end: up"])
        self.assertFalse(list(self.run.glob("*.tmp")))

    def test_action_failure_is_recorded_and_run_continues(self):
        actions, calls = self.fake_actions(fail="origin-503")
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        steps = [(0, "origin-503", "down"), (0, "origin-200", "up")]
        timeline = chaos.run_schedule("origin-stale", 90, steps, self.run, stop, None, actions,
                                      annotator=lambda *a: True)
        self.assertEqual(timeline["faults"][0]["detail"], "error: boom")
        self.assertIn("origin-200", calls)

    def test_stop_before_first_fault_skips_steps_but_restores(self):
        actions, calls = self.fake_actions()
        stop = threading.Event()
        stop.set()
        steps = [(1000, "origin-503", "down")]
        timeline = chaos.run_schedule("origin-stale", 90, steps, self.run, stop, None, actions,
                                      annotator=lambda *a: True)
        self.assertEqual([f["action"] for f in timeline["faults"]], ["restore"])
        self.assertNotIn("origin-503", calls)

    def test_restore_runs_even_when_a_step_raises_outside_actions(self):
        actions, calls = self.fake_actions()

        def broken_annotator(*_):
            raise KeyError("annotator bug")

        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        with self.assertRaises(KeyError):
            chaos.run_schedule("origin-stale", 90, [(0, "origin-503", "down")], self.run, stop,
                               None, actions, annotator=broken_annotator)
        self.assertIn("restore", calls)
        self.assertTrue((self.run / "faults.json").exists())


class AnnotateTest(unittest.TestCase):
    def test_no_grafana_url_returns_false(self):
        self.assertFalse(chaos.annotate(None, "origin-stale", "x", 1))

    def test_unreachable_grafana_warns_and_returns_false(self):
        self.assertFalse(chaos.annotate("http://127.0.0.1:1", "origin-stale", "x", 1, auth="a:b"))


class FaultTimelineReportTest(unittest.TestCase):
    def test_report_includes_fault_timeline_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "run.json").write_text(json.dumps({"scenario": "origin-stale"}))
            (run / "faults.json").write_text(json.dumps({
                "faults": [{"action": "origin-503", "scheduled_offset": 60,
                            "actual_epoch_ms": 1700000000000, "detail": "origin default case status 503"}]}))
            text = report.render(run)
            self.assertIn("## Fault timeline", text)
            self.assertIn("origin-503", text)

    def test_report_omits_fault_section_without_faults_json(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "run.json").write_text(json.dumps({"scenario": "smoke"}))
            self.assertNotIn("## Fault timeline", report.render(run))


if __name__ == "__main__":
    unittest.main()
