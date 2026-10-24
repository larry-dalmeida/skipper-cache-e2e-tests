import random
import re
import unittest
from collections import Counter
from pathlib import Path

import catalogue

ROUTES_FILE = Path(__file__).resolve().parent / "routes.eskip"
ROUTE_LINE = re.compile(r'^(\w+): (Path|PathRegexp)\("([^"]*)"\)')


def parse_routes(text):
    routes = []
    for line in text.splitlines():
        match = ROUTE_LINE.match(line)
        if match:
            routes.append(match.groups())
    return routes


def matching_route_ids(routes, path):
    ids = []
    for route_id, kind, pattern in routes:
        request_path = path.partition("?")[0]
        if kind == "Path" and request_path == pattern:
            ids.append(route_id)
        if kind == "PathRegexp" and re.search(pattern, request_path):
            ids.append(route_id)
    return ids


class Snapshot(unittest.TestCase):
    def test_excluded_external_fi_is_not_in_catalogue(self):
        paths = {e.path for e in catalogue.build_entries()}
        self.assertNotIn("/api/contentful/unify-footers/external/en_FI", paths)
        self.assertIn("/api/contentful/unify-footers/external/fi_FI", paths)

    def test_contentful_weights_match_snapshot_minus_exclusions(self):
        snapshot = catalogue._snapshot()
        expected = sum(item["requests"]
                       for key in catalogue.CONTENTFUL_FAMILIES
                       for item in snapshot[key]
                       if item["path"] not in catalogue.EXCLUDED_PATHS)
        actual = sum(e.weight for e in catalogue.build_entries()
                     if e.family.startswith("contentful_"))
        self.assertAlmostEqual(actual, expected)

    def test_shop_traffic_is_the_configured_share(self):
        entries = catalogue.build_entries()
        contentful = sum(e.weight for e in entries if e.family.startswith("contentful_"))
        shop = sum(e.weight for e in entries if e.family.startswith("shop_"))
        self.assertAlmostEqual(shop / (contentful + shop), catalogue.SHOP_SHARE)

    def test_shop_endpoints_exist_for_every_footer_locale(self):
        entries = catalogue.build_entries()
        footer_locales = {e.locale for e in entries if e.family == "contentful_footers_direct"}
        shop_locales = {e.locale for e in entries if e.family.startswith("shop_")}
        self.assertEqual(footer_locales, shop_locales)
        self.assertEqual(len(footer_locales), 27)


class Routes(unittest.TestCase):
    def test_every_entry_is_served_by_its_own_route(self):
        entries = catalogue.build_entries()
        routes = parse_routes(catalogue.render_routes(entries))
        for entry in entries:
            ids = matching_route_ids(routes, entry.path)
            self.assertIn(entry.route_id, ids, entry.path)

    def test_routes_are_unique_per_family_and_locale(self):
        routes = parse_routes(catalogue.render_routes(catalogue.build_entries()))
        ids = [route_id for route_id, _, _ in routes]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fallback_route_is_first(self):
        first = catalogue.render_routes(catalogue.build_entries()).splitlines()[0]
        self.assertTrue(first.startswith("r0: * -> "))

    def test_committed_routes_file_matches_catalogue(self):
        rendered = catalogue.render_routes(catalogue.build_entries())
        self.assertEqual(ROUTES_FILE.read_text(), rendered,
                         "routes.eskip is stale: run `python3 catalogue.py routes > routes.eskip`")


class Picking(unittest.TestCase):
    def test_pick_follows_weights(self):
        entries = [catalogue.Entry("a", "x", "/a", 3.0), catalogue.Entry("b", "x", "/b", 1.0)]
        picker = catalogue.Catalogue(entries)
        rng = random.Random(11)
        counts = Counter(picker.pick(rng).family for _ in range(20000))
        self.assertAlmostEqual(counts["a"] / 20000, 0.75, delta=0.02)

    def test_families_lists_every_family_once(self):
        picker = catalogue.Catalogue(catalogue.build_entries())
        self.assertEqual(len(picker.families()), len(set(picker.families())))
        self.assertEqual(len(picker.families()), 9)


if __name__ == "__main__":
    unittest.main()
