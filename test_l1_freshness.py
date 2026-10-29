import unittest

from verify_l1_freshness import delta, parse_metrics, regression_errors


class L1FreshnessAssertions(unittest.TestCase):
    def observations(self):
        return {name: {"response": {"status": 200, "cache_status": "HIT",
                                   "body": {"version": "B"}}, "origin_delta": 0}
                for name in ("a_only_if_cached", "a_normal")}

    def test_fresh_shared_hit_passes(self):
        self.assertEqual(regression_errors(self.observations()), [])

    def test_retained_l1_504_is_failure(self):
        observations = self.observations()
        observations["a_only_if_cached"]["response"] = {
            "status": 504, "cache_status": "MISS", "body": None}
        self.assertIn("a_only_if_cached", regression_errors(observations)[0])

    def test_correct_body_with_extra_origin_request_is_failure(self):
        observations = self.observations()
        observations["a_normal"]["origin_delta"] = 1
        self.assertIn("a_normal", regression_errors(observations)[0])

    def test_old_body_and_miss_are_failures(self):
        for field, value in (("body", {"version": "A"}), ("cache_status", "MISS")):
            with self.subTest(field=field):
                observations = self.observations()
                observations["a_normal"]["response"][field] = value
                self.assertEqual(len(regression_errors(observations)), 1)

    def test_metric_delta_handles_absent_counters(self):
        self.assertEqual(delta({"old": 1}, {"old": 1, "new": 2}), {"new": 2})

    def test_metrics_use_existing_custom_counter_format(self):
        self.assertEqual(parse_metrics(
            '# HELP skipper_custom_total ...\n'
            'skipper_custom_total{key="cache.l1_hit"} 2\n'
            'skipper_custom_total{key="cache.coalesce_error"} 1\n'
            'skipper_custom_total{key="other"} 9\n'),
            {"cache.l1_hit": 2, "cache.coalesce_error": 1})
