import json
import unittest
from pathlib import Path


class ClientDashboardContract(unittest.TestCase):
    def setUp(self):
        dashboard = json.loads((Path(__file__).parent /
                                "dashboards/cache-performance.json").read_text())
        self.panels = {panel["id"]: panel for panel in dashboard["panels"]}

    def test_universal_client_rates_and_denominators_exclude_listener_replays(self):
        for panel_id in (44, 7, 48, 50, 55):
            with self.subTest(panel_id=panel_id):
                expression = self.panels[panel_id]["targets"][0]["expr"]
                self.assertIn('key="client.requests"', expression)
                self.assertNotIn("skipper_serve_route_count", expression)
                self.assertNotIn("skipper_cache_route_result_total", expression)
                self.assertIn('up{job="skipper-local"} == 1', expression)
                self.assertNotIn("or vector(0)", expression)
                if panel_id in (48, 50, 55):
                    self.assertIn("mock_origin_requests_total", expression)
                    self.assertIn("> 0)", expression)

    def test_ratio_units_and_amplification_semantics_are_preserved(self):
        for panel_id in (48, 50, 55):
            self.assertEqual(self.panels[panel_id]["fieldConfig"]["defaults"]["unit"],
                             "percentunit")
        self.assertNotIn("clamp(", self.panels[55]["targets"][0]["expr"])
        self.assertIn("all-caller", self.panels[55]["description"].lower())
        self.assertIn("not cache hit ratio", self.panels[55]["description"])

    def test_refresh_and_shared_l1_dimensions_are_not_double_counted(self):
        self.assertIn("sum by (family, outcome)", self.panels[53]["targets"][0]["expr"])
        for panel_id in (25, 58, 59):
            for target in self.panels[panel_id]["targets"]:
                self.assertIn("max by (instance)", target["expr"])
                self.assertNotIn("sum(", target["expr"])
