import unittest

from dashboard import dashboard, fresh_ratio


class DashboardTests(unittest.TestCase):
    def test_overall_excludes_tier_and_error_results(self):
        for panel in dashboard["panels"]:
            if panel["title"].startswith("Overall"):
                self.assertIn('result=~"hit|miss"', panel["targets"][0]["expr"])

    def test_tier_labels_and_consistent_window(self):
        for tier in ("L1", "L2"):
            panel = next(p for p in dashboard["panels"] if p["title"] ==
                         tier + " Hits by Status Code Class")
            expr = panel["targets"][0]["expr"]
            self.assertIn('result="' + tier.lower() + '_hit"', expr)
            self.assertIn("sum by (code_class)", expr)
            self.assertIn("[$__rate_interval]", expr)
            self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "reqps")

    def test_composition_preserves_volume_and_line_comparison(self):
        line, stacked = [p for p in dashboard["panels"]
                         if p["title"].startswith("Cache Hit vs Miss")]
        self.assertEqual(line["title"], "Cache Hit vs Miss Rate")
        self.assertEqual(line["targets"], stacked["targets"])
        self.assertEqual(line["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"],
                         "none")
        self.assertEqual(stacked["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"],
                         "normal")

    def test_every_query_is_scoped_and_datasource_templated(self):
        for panel in dashboard["panels"]:
            if panel["type"] == "row":
                continue
            self.assertEqual(panel["datasource"]["uid"], "${datasource}")
            for target in panel["targets"]:
                self.assertIn('job="skipper-local"', target["expr"])
                self.assertIn('instance=~"${instance:regex}"', target["expr"])
                self.assertEqual(target["datasource"]["uid"], "${datasource}")

    def test_fresh_hit_denominator_and_no_traffic_guard(self):
        self.assertIn("cache\\\\.(hit|miss|stale)", fresh_ratio)
        self.assertIn("[$__rate_interval]", fresh_ratio)
        self.assertIn("> 0", fresh_ratio)
        self.assertIn("0 *", fresh_ratio)
        self.assertIn("min(up{", fresh_ratio)
        self.assertNotIn("vector(0)", fresh_ratio)
        for excluded in ("coalesce_error", "storage_error", "l1_hit", "l2_hit"):
            self.assertNotIn(excluded, fresh_ratio)

    def test_composition_uses_complete_flat_outcome_schema(self):
        for panel in dashboard["panels"]:
            if panel["title"].startswith("Cache Hit vs Miss"):
                expr = panel["targets"][0]["expr"]
                self.assertIn("skipper_custom_total", expr)
                self.assertIn("cache\\\\.(hit|miss)", expr)
                self.assertIn("[$__rate_interval]", expr)

    def test_lazy_faults_not_filled_and_queue_is_gauge(self):
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                if panel["fieldConfig"]["defaults"]["noValue"] == "Not exercised / no samples":
                    self.assertNotIn("or vector", target["expr"])
        queue = next(p for p in dashboard["panels"] if p["title"] == "Revalidation queue depth")
        self.assertIn("skipper_custom_gauges", queue["targets"][0]["expr"])
        self.assertNotIn("rate(", queue["targets"][0]["expr"])


if __name__ == "__main__":
    unittest.main()
