import json
import random
import unittest
from collections import Counter

import locust_workload as wl


def echo(cohort, index, **overrides):
    headers = {"Host": [wl.HOST_HEADER], "Authorization": [wl.AUTHORIZATION],
               "Accept-Language": [wl.COHORTS[cohort]]}
    body = {"url": "http://127.0.0.1:9999" + wl.key_path(cohort, index),
            "headers": headers}
    body.update(overrides)
    return json.dumps(body)


class Fixtures(unittest.TestCase):
    def test_key_space_has_one_primary_twenty_related_thirty_two_cooler(self):
        classes = Counter(wl.key_class(i) for i in range(wl.KEYS_PER_COHORT))
        self.assertEqual(classes, {"primary": 1, "related": 20, "cooler": 32})

    def test_out_of_range_key_is_rejected(self):
        with self.assertRaises(ValueError):
            wl.key_path("swr", wl.KEYS_PER_COHORT)
        with self.assertRaises(ValueError):
            wl.key_path("unknown", 0)

    def test_pick_key_respects_70_20_10_within_tolerance(self):
        rng = random.Random(7)
        counts = Counter(wl.pick_key(rng, "swr")[0] for _ in range(20000))
        self.assertAlmostEqual(counts["primary"] / 20000, 0.70, delta=0.02)
        self.assertAlmostEqual(counts["related"] / 20000, 0.20, delta=0.02)
        self.assertAlmostEqual(counts["cooler"] / 20000, 0.10, delta=0.02)

    def test_stat_names_are_fixed_and_not_per_key(self):
        names = {wl.stat_name(c, k) for c in wl.COHORTS
                 for k in ("primary", "related", "cooler")}
        self.assertEqual(len(names), 6)


class Validation(unittest.TestCase):
    def test_valid_echo_passes(self):
        self.assertIsNone(wl.validate_response(200, echo("swr", 3), "swr", 3))

    def test_non_200_is_status(self):
        self.assertEqual(wl.validate_response(502, echo("swr", 3), "swr", 3), "status")

    def test_non_json_is_body(self):
        self.assertEqual(wl.validate_response(200, b"<html>", "swr", 3), "body")
        self.assertEqual(wl.validate_response(200, b"[1]", "swr", 3), "body")

    def test_wrong_key_is_identity(self):
        self.assertEqual(wl.validate_response(200, echo("swr", 4), "swr", 3), "identity")

    def test_wrong_cohort_language_is_identity(self):
        body = echo("hard", 3)
        self.assertEqual(wl.validate_response(200, body, "swr", 3), "identity")

    def test_masked_authorization_echo_is_accepted(self):
        data = json.loads(echo("swr", 3))
        data["headers"]["Authorization"] = ["******"]
        self.assertIsNone(wl.validate_response(200, json.dumps(data), "swr", 3))

    def test_missing_authorization_echo_is_identity(self):
        data = json.loads(echo("swr", 3))
        del data["headers"]["Authorization"]
        self.assertEqual(wl.validate_response(200, json.dumps(data), "swr", 3), "identity")

    def test_failure_category_maps_known_and_unknown(self):
        self.assertIsNone(wl.failure_category(None))
        self.assertEqual(wl.failure_category(Exception("identity")), "identity")
        self.assertEqual(wl.failure_category(Exception("boom")), "exception")

    def test_options_bounds(self):
        wl.validate_options(5, 0.5, 1.5, 5.0)
        for args in [(0, 0.5, 1.5, 5.0), (51, 0.5, 1.5, 5.0), (5, 2, 1, 5.0),
                     (5, 0.5, 1.5, 0), (5, 0.5, 1.5, 31)]:
            with self.assertRaises(ValueError):
                wl.validate_options(*args)


class Guard(unittest.TestCase):
    def test_identity_mismatch_latches_immediately(self):
        guard = wl.SafetyGuard()
        self.assertTrue(guard.record("identity"))
        self.assertEqual(guard.latched, "identity mismatch")
        self.assertFalse(guard.record("identity"))

    def test_failure_ratio_needs_minimum_samples(self):
        guard = wl.SafetyGuard(min_samples=10, max_ratio=0.05)
        for _ in range(5):
            self.assertFalse(guard.record("status"))
        self.assertIsNone(guard.latched)

    def test_failure_ratio_latches_above_threshold(self):
        guard = wl.SafetyGuard(min_samples=50, max_ratio=0.05)
        latched = False
        for _ in range(50):
            latched = guard.record("status") or latched
        self.assertTrue(latched)
        self.assertEqual(guard.latched, "failure ratio")

    def test_latch_survives_window_reset(self):
        guard = wl.SafetyGuard()
        guard.record("identity")
        guard.start_window()
        self.assertEqual(guard.latched, "identity mismatch")
        self.assertEqual(guard.requests, 0)

    def test_success_only_run_never_latches(self):
        guard = wl.SafetyGuard()
        for _ in range(500):
            self.assertFalse(guard.record(None))
        self.assertIsNone(guard.latched)


class Ledger(unittest.TestCase):
    def test_window_totals_and_history_bound(self):
        ledger = wl.WindowLedger(limit=2)
        for n in range(3):
            ledger.begin(float(n))
            window = ledger.end(float(n) + 1,
                                [{"name": "swr/primary", "requests": 4, "failures": 1,
                                  "p95_ms": 3.0}])
            self.assertEqual(window["requests"], 4)
        self.assertEqual(len(ledger.history), 2)
        self.assertEqual(ledger.windows_dropped, 1)
        self.assertEqual(ledger.lifetime_requests, 12)
        self.assertEqual(ledger.lifetime_failures, 3)

    def test_end_without_begin_is_ignored(self):
        self.assertIsNone(wl.WindowLedger().end(1.0, []))


class CohortTargets(unittest.TestCase):
    def test_targets_stay_in_range_and_under_ceiling(self):
        rng = random.Random(3)
        for _ in range(2000):
            targets = wl.pick_cohort_targets(rng, wl.MAX_VUS_CEILING)
            self.assertEqual(set(targets), set(wl.COHORTS))
            for count in targets.values():
                self.assertTrue(10 <= count <= 30)
            self.assertLessEqual(sum(targets.values()), wl.MAX_VUS_CEILING)

    def test_targets_vary_across_draws(self):
        rng = random.Random(5)
        seen = {tuple(sorted(wl.pick_cohort_targets(rng, wl.MAX_VUS_CEILING).items()))
                for _ in range(50)}
        self.assertGreater(len(seen), 1)

    def test_ceiling_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            wl.pick_cohort_targets(random.Random(1), 19)


if __name__ == "__main__":
    unittest.main()
