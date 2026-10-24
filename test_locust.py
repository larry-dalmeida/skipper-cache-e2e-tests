import json
import random
import unittest
from collections import Counter

import catalogue
import locust_workload as wl

ENTRY = catalogue.Entry("shop_navigation", "pl_PL", "/shop/pl_PL/api/navigation", 1.0)


def echo(cohort, entry, **overrides):
    """Fixture as mock_origin.py returns it for a request on this entry."""
    path, _, query = entry.path.partition("?")
    body = {"version": "A", "path": path, "query": query, "host": wl.HOST_HEADER,
            "tokenClass": wl.ORIGIN_TOKEN_CLASS, "language": wl.COHORTS[cohort]}
    body.update(overrides)
    return json.dumps(body)


class StatNames(unittest.TestCase):
    def test_stat_names_are_cohort_by_family_not_per_path(self):
        families = catalogue.Catalogue(catalogue.build_entries()).families()
        names = {wl.stat_name(c, f) for c in wl.COHORTS for f in families}
        self.assertEqual(len(names), len(wl.COHORTS) * len(families))


class Validation(unittest.TestCase):
    def test_valid_echo_passes(self):
        self.assertIsNone(wl.validate_response(200, echo("swr", ENTRY), "swr", ENTRY))

    def test_query_entry_passes(self):
        entry = catalogue.Entry("shop_search", "pl_PL", "/shop/pl_PL/api/search?q=jacket", 1.0)
        self.assertIsNone(wl.validate_response(200, echo("hard", entry), "hard", entry))

    def test_non_200_is_status(self):
        self.assertEqual(wl.validate_response(502, echo("swr", ENTRY), "swr", ENTRY), "status")

    def test_non_json_is_body(self):
        self.assertEqual(wl.validate_response(200, b"<html>", "swr", ENTRY), "body")
        self.assertEqual(wl.validate_response(200, b"[1]", "swr", ENTRY), "body")

    def test_wrong_path_is_identity(self):
        other = catalogue.Entry("shop_navigation", "pl_PL", "/shop/pl_PL/api/promotions", 1.0)
        self.assertEqual(wl.validate_response(200, echo("swr", other), "swr", ENTRY), "identity")

    def test_wrong_query_is_identity(self):
        entry = catalogue.Entry("shop_search", "pl_PL", "/shop/pl_PL/api/search?q=jacket", 1.0)
        body = echo("swr", catalogue.Entry("shop_search", "pl_PL",
                                           "/shop/pl_PL/api/search?q=dress", 1.0))
        self.assertEqual(wl.validate_response(200, body, "swr", entry), "identity")

    def test_wrong_cohort_language_is_identity(self):
        body = echo("hard", ENTRY)
        self.assertEqual(wl.validate_response(200, body, "swr", ENTRY), "identity")

    def test_wrong_token_class_is_identity(self):
        body = echo("swr", ENTRY, tokenClass="alpha")
        self.assertEqual(wl.validate_response(200, body, "swr", ENTRY), "identity")

    def test_wrong_host_is_identity(self):
        body = echo("swr", ENTRY, host="other.local")
        self.assertEqual(wl.validate_response(200, body, "swr", ENTRY), "identity")

    def test_failure_category_maps_known_and_unknown(self):
        self.assertIsNone(wl.failure_category(None))
        self.assertEqual(wl.failure_category(Exception("identity")), "identity")
        self.assertEqual(wl.failure_category(Exception("boom")), "exception")

    def test_options_bounds(self):
        wl.validate_options(5, 0.5, 1.5, 5.0)
        for args in [(0, 0.5, 1.5, 5.0), (301, 0.5, 1.5, 5.0), (5, 2, 1, 5.0),
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
                                [{"name": "swr/contentful_spaces", "requests": 4,
                                  "failures": 1, "p95_ms": 3.0}])
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
                self.assertTrue(20 <= count <= 150)
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
