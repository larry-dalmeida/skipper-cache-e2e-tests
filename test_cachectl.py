import unittest

import cachectl


def plan(*argv):
    argv = list(argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    return cachectl.build_commands(cachectl.parser().parse_args(argv), extra)


class CachectlTests(unittest.TestCase):
    def test_fault_origin_down_scales_mock_backend_in_pinned_context(self):
        [command] = plan("fault", "origin", "down")
        self.assertEqual(command, ["kubectl", "--context", "minikube", "-n", "cache-e2e",
                                   "scale", "deploy/mock-backend", "--replicas=0"])

    def test_fault_valkey_up_scales_then_waits_for_rollout(self):
        scale, rollout = plan("fault", "valkey", "up")
        self.assertEqual(scale[-2:], ["deploy/valkey", "--replicas=1"])
        self.assertEqual(rollout[5:8], ["rollout", "status", "deploy/valkey"])

    def test_skipper_restart_stops_then_starts_with_checkout(self):
        stop, start = plan("skipper", "restart", "--", "/tmp/skipper")
        self.assertEqual(stop[-1], "skipper")
        self.assertTrue(stop[1].endswith("teardown.sh"))
        self.assertTrue(start[1].endswith("run-skipper.sh"))
        self.assertEqual(start[2:], ["/tmp/skipper"])

    def test_check_verify_passes_through_arguments(self):
        [command] = plan("check", "verify", "--", "--label", "x", "--sha", "abc")
        self.assertTrue(command[1].endswith("verify.py"))
        self.assertEqual(command[2:], ["--label", "x", "--sha", "abc"])

    def test_check_unit_runs_all_offline_suites(self):
        [command] = plan("check", "unit")
        self.assertEqual(command[-4:-1], ["test_harness", "test_locust", "test_dashboard"])

    def test_load_starts_locust_supervisor(self):
        [command] = plan("load")
        self.assertTrue(command[1].endswith("lifecycle.py"))
        self.assertEqual(command[2], "locust")

    def test_logs_starts_log_shipper_supervisor(self):
        [command] = plan("logs")
        self.assertTrue(command[1].endswith("lifecycle.py"))
        self.assertEqual(command[2], "logs")

    def test_stack_down_uses_teardown_not_namespace_delete(self):
        [command] = plan("stack", "down")
        self.assertTrue(command[1].endswith("teardown.sh"))
        self.assertNotIn("delete", command)

    def test_unknown_fault_target_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            plan("fault", "database", "down")


if __name__ == "__main__":
    unittest.main()
