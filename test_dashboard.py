import unittest

from dashboard import dashboard


class DashboardTests(unittest.TestCase):
    def test_overall_excludes_tier_and_error_results(self):
        for panel in dashboard["panels"]:
            if panel["title"].startswith(("Overall", "Cache Hit vs Miss")):
                self.assertIn('result=~"hit|miss"', panel["targets"][0]["expr"])

    def test_tier_labels_and_consistent_window(self):
        for tier in ("L1", "L2"):
            panel = next(p for p in dashboard["panels"] if p["title"] ==
                         tier + " Hits by Status Code Class")
            expr = panel["targets"][0]["expr"]
            self.assertIn('result="' + tier.lower() + '_hit"', expr)
            self.assertIn("sum by (code_class)", expr)
            self.assertIn("[5m]", expr)
            self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "reqps")

    def test_composition_preserves_volume_and_line_comparison(self):
        line, stacked = [p for p in dashboard["panels"]
                         if p["title"].startswith("Cache Hit vs Miss")]
        self.assertEqual(line["targets"], stacked["targets"])
        self.assertEqual(line["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"],
                         "none")
        self.assertEqual(stacked["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"],
                         "normal")

    def test_every_query_is_scoped_and_datasource_templated(self):
        for panel in dashboard["panels"]:
            self.assertEqual(panel["datasource"]["uid"], "${datasource}")
            for target in panel["targets"]:
                self.assertIn('job="skipper-local"', target["expr"])
                self.assertIn('instance=~"${instance:regex}"', target["expr"])
                self.assertEqual(target["datasource"]["uid"], "${datasource}")


if __name__ == "__main__":
    unittest.main()
