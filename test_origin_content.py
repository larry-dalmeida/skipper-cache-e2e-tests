import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "origin"))

import retail_workload  # noqa: E402
from retail_workload import BUILDERS, CACHE_CONTROL, FAMILY_SIZES, ORIGIN_ENDPOINTS, content  # noqa: E402

SAMPLES = {
    "contentful_spaces": ("cms", "/spaces/8397t4ds1z4f/environments/master/entries", ""),
    "contentful_footers_direct": ("cms", "/api/contentful/unify-footers/de_DE", ""),
    "contentful_footers_external": ("cms", "/api/contentful/unify-footers/external/en_GB", ""),
    "contentful_vouchers": ("cms", "/api/contentful/voucher-messages/pl_PL", ""),
    "shop_products": ("gateway", "/shop/de_DE/api/products/sku-1001", ""),
    "shop_categories": ("gateway", "/shop/de_DE/api/categories/sale", ""),
    "shop_search": ("gateway", "/shop/de_DE/api/search", "q=jacket&page=1"),
    "shop_navigation": ("gateway", "/shop/de_DE/api/navigation", ""),
    "shop_promotions": ("gateway", "/shop/de_DE/api/promotions/banner", ""),
    "shop_personal": ("gateway", "/shop/de_DE/api/cart", ""),
    "shop_mutations": ("gateway", "/shop/de_DE/api/cart/items", "sku=sku-1001&qty=2"),
}
SPACES_PATH = "/spaces/8397t4ds1z4f/environments/master/entries"
MIB = 1024 * 1024


class RetailContentTest(unittest.TestCase):
    def test_same_input_yields_identical_bytes_and_etag(self):
        for family, (_, path, query) in SAMPLES.items():
            first = content(family, path, query)
            second = content(family, path, query)
            self.assertEqual(first, second, family)
            self.assertEqual(first[2], second[2])
            self.assertEqual(first[1]["ETag"], second[1]["ETag"])

    def test_query_order_does_not_change_output(self):
        a = content("shop_search", "/shop/de_DE/api/search", "q=jacket&page=2")
        b = content("shop_search", "/shop/de_DE/api/search", "page=2&q=jacket")
        self.assertEqual(a, b)

    def test_different_urls_yield_different_bodies(self):
        a = content("shop_products", "/shop/de_DE/api/products/sku-1001")[2]
        b = content("shop_products", "/shop/de_DE/api/products/sku-1002")[2]
        self.assertNotEqual(a, b)

    def test_etag_is_quoted_sha256_of_body(self):
        status, headers, body = content("shop_categories", "/shop/de_DE/api/categories/sale")
        self.assertEqual(headers["ETag"], f'"{hashlib.sha256(body).hexdigest()}"')

    def test_sizes_within_target_range(self):
        for family, (_, path, query) in SAMPLES.items():
            _, _, body = content(family, path, query)
            low, high = FAMILY_SIZES[family]
            self.assertTrue(low <= len(body) <= high, f"{family}: {len(body)} not in [{low}, {high}]")

    def test_spaces_2mb_variant_selected_by_query(self):
        _, headers, body = content("contentful_spaces", SPACES_PATH, "size=2mb")
        self.assertTrue(1.9 * MIB <= len(body) <= 2.1 * MIB, len(body))
        self.assertEqual(headers["Content-Type"], "application/json")
        payload = json.loads(body)
        self.assertEqual(payload["synthetic"]["variant"], "oversized-2mb")
        self.assertIn("SYNTHETIC 2MB", payload["items"][-1]["fields"]["title"]["en-GB"])

    def test_spaces_2mb_is_deterministic_and_normal_size_unchanged(self):
        self.assertEqual(content("contentful_spaces", SPACES_PATH, "size=2mb"),
                         content("contentful_spaces", SPACES_PATH, "size=2mb"))
        normal = content("contentful_spaces", SPACES_PATH)[2]
        self.assertLessEqual(len(normal), 60_000)
        self.assertNotIn(b"synthetic", normal)
        self.assertNotEqual(content("contentful_spaces", SPACES_PATH, "size=2mb")[2], normal)

    def test_contentful_envelope_and_locale_keys(self):
        for family, path in (("contentful_spaces", SPACES_PATH),
                             ("contentful_footers_direct", "/api/contentful/unify-footers/pl_PL"),
                             ("contentful_vouchers", "/api/contentful/voucher-messages/pl_PL")):
            payload = json.loads(content(family, path)[2])
            self.assertEqual(payload["sys"]["type"], "Array", family)
            self.assertGreaterEqual(payload["total"], len(payload["items"]), family)
            self.assertEqual(payload["limit"], len(payload["items"]), family)
            self.assertEqual(payload["skip"], 0, family)
            self.assertIn("Asset", payload["includes"], family)
            self.assertIn("Entry", payload["includes"], family)
            item = payload["items"][0]
            self.assertIn("updatedAt", item["sys"], family)
            self.assertEqual(item["sys"]["type"], "Entry", family)
            locales = set(item["fields"]["title"])
            self.assertEqual(len(locales), 1, family)
            self.assertRegex(next(iter(locales)), r"^[a-z]{2}-[A-Z]{2}$", family)

    def test_footer_locale_key_follows_path(self):
        payload = json.loads(content("contentful_footers_direct", "/api/contentful/unify-footers/pl_PL")[2])
        self.assertEqual(payload["items"][0]["sys"]["locale"], "pl-PL")
        self.assertIn("pl-PL", payload["items"][0]["fields"]["title"])

    def test_includes_resolve_item_links(self):
        payload = json.loads(content("contentful_spaces", SPACES_PATH)[2])
        assets = {asset["sys"]["id"] for asset in payload["includes"]["Asset"]}
        entries = {entry["sys"]["id"] for entry in payload["includes"]["Entry"]}
        for item in payload["items"]:
            hero = item["fields"]["heroImage"]["en-GB"]["sys"]["id"]
            self.assertIn(hero, assets)
            for link in item["fields"]["links"]["en-GB"]:
                self.assertIn(link["sys"]["id"], entries)

    def test_no_image_or_static_families(self):
        for family in BUILDERS:
            self.assertFalse(any(word in family for word in ("image", "static", "asset")), family)
        for _, _, body in (content(f, p, q) for f, (_, p, q) in SAMPLES.items()):
            self.assertFalse(body.startswith(b"\x89PNG") or body.startswith(b"\xff\xd8"))

    def test_mutation_is_small_no_store_and_echoes_request(self):
        status, headers, body = content("shop_mutations", "/shop/de_DE/api/cart/items", "sku=sku-1003&qty=3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(200 <= len(body) <= 2000)
        payload = json.loads(body)
        self.assertTrue(payload["cartId"].startswith("cart-"))
        self.assertEqual(payload["items"][0]["fields"]["sku"], "sku-1003")
        self.assertEqual(payload["items"][0]["fields"]["qty"], 3)

    def test_mutation_rejects_unsafe_sku(self):
        _, _, body = content("shop_mutations", "/shop/de_DE/api/cart/items", "sku=<script>")
        self.assertEqual(json.loads(body)["items"][0]["fields"]["sku"], "sku-1001")

    def test_cache_control_per_family(self):
        for family, (_, path, query) in SAMPLES.items():
            self.assertEqual(content(family, path, query)[1]["Cache-Control"], CACHE_CONTROL[family])
        self.assertEqual(CACHE_CONTROL["shop_personal"], "private, max-age=30")

    def test_vary_only_on_locale_paths(self):
        self.assertEqual(content("shop_products", "/shop/de_DE/api/products/sku-1001")[1]["Vary"],
                         "Accept-Language")
        self.assertNotIn("Vary", content("contentful_spaces", SAMPLES["contentful_spaces"][1])[1])

    def test_version_changes_body(self):
        a = content("contentful_vouchers", "/api/contentful/voucher-messages/pl_PL", version="A")[2]
        b = content("contentful_vouchers", "/api/contentful/voucher-messages/pl_PL", version="B")[2]
        self.assertNotEqual(a, b)

    def test_negative_family_is_500(self):
        status, headers, _ = content("negative", "/shop/api/negative-fixture")
        self.assertEqual(status, 500)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_every_endpoint_family_is_known(self):
        for endpoint in ORIGIN_ENDPOINTS.values():
            self.assertIn(endpoint.family, BUILDERS)

    def test_contentful_paths_cover_measured_snapshot(self):
        snapshot = os.path.join(ROOT, "contentful-cache-paths.json")
        with open(snapshot, encoding="utf-8") as handle:
            measured = json.load(handle)["families"]
        for family_paths in measured.values():
            for item in family_paths:
                self.assertIn(("cms", item["path"]), ORIGIN_ENDPOINTS, item["path"])

    def test_retail_mode_import_succeeds(self):
        import mock_origin  # noqa: F401


if __name__ == "__main__":
    unittest.main()
