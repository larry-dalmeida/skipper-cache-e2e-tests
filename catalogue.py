#!/usr/bin/env python3
"""Endpoint catalogue for the cache workload, and the Skipper routes that serve it.

Contentful families come from contentful-cache-paths.json, a snapshot of production request
counts (Dash0, 6h window). Each path keeps its real count as its traffic weight. The mock
online-shop endpoints are cacheable storefront GETs generated for every Contentful locale.
They take SHOP_SHARE of total traffic, split by SHOP_FAMILIES shares.

Each family and locale gets its own Skipper route, so per-route metrics carry both labels:
route id "<family>__<locale>". `python3 catalogue.py > routes.eskip` renders the routes.
"""
import bisect
import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent / "contentful-cache-paths.json"
ORIGIN = "http://127.0.0.1:8081"
CACHE_ARGS = '"30s", "10s", "15s"'
SHOP_SHARE = 0.25
# Contentful paths the snapshot marks as not cache-backed (upstream 500s).
EXCLUDED_PATHS = {"/api/contentful/unify-footers/external/en_FI"}
CONTENTFUL_FAMILIES = {
    "spaces": "contentful_spaces",
    "unifyFootersDirect": "contentful_footers_direct",
    "unifyFootersExternal": "contentful_footers_external",
    "voucherMessages": "contentful_vouchers",
}
# family, path template, share of shop traffic, identifiers (one entry per identifier).
SHOP_FAMILIES = (
    ("shop_products", "/shop/{locale}/api/products/{id}", 0.40,
     ("sku-1001", "sku-1002", "sku-1003", "sku-1004", "sku-1005", "sku-1006", "sku-1007", "sku-1008")),
    ("shop_categories", "/shop/{locale}/api/categories/{id}", 0.20,
     ("women-shoes", "men-clothing", "sale", "new-in")),
    ("shop_search", "/shop/{locale}/api/search?q={id}", 0.15,
     ("sneakers", "jacket", "dress")),
    ("shop_navigation", "/shop/{locale}/api/navigation", 0.15, ("main",)),
    ("shop_promotions", "/shop/{locale}/api/promotions/banner", 0.10, ("banner",)),
)


class Entry:
    __slots__ = ("family", "locale", "path", "weight")

    def __init__(self, family, locale, path, weight):
        self.family = family
        self.locale = locale
        self.path = path
        self.weight = weight

    @property
    def route_id(self):
        return f"{self.family}__{self.locale}"


def _snapshot():
    return json.loads(SNAPSHOT.read_text())["families"]


def _locale(path):
    return path.rsplit("/", 1)[1]


def build_entries(snapshot=None):
    families = snapshot or _snapshot()
    entries = []
    for key, family in CONTENTFUL_FAMILIES.items():
        for item in families[key]:
            if item["path"] in EXCLUDED_PATHS:
                continue
            locale = "all" if key == "spaces" else _locale(item["path"])
            entries.append(Entry(family, locale, item["path"], item["requests"]))
    contentful_total = sum(entry.weight for entry in entries)
    footers = [item for item in families["unifyFootersDirect"]]
    footers_total = sum(item["requests"] for item in footers)
    shop_total = contentful_total * SHOP_SHARE / (1 - SHOP_SHARE)
    for item in footers:
        locale = _locale(item["path"])
        locale_share = item["requests"] / footers_total
        for family, template, family_share, identifiers in SHOP_FAMILIES:
            for identifier in identifiers:
                weight = shop_total * locale_share * family_share / len(identifiers)
                path = template.format(locale=locale, id=identifier)
                entries.append(Entry(family, locale, path, weight))
    return entries


class Catalogue:
    """Weighted picker over all entries; pick(rng) returns one Entry."""

    def __init__(self, entries):
        self.entries = entries
        self.cumulative = []
        total = 0.0
        for entry in entries:
            total += entry.weight
            self.cumulative.append(total)
        self.total = total

    def pick(self, rng):
        return self.entries[bisect.bisect_left(self.cumulative, rng.random() * self.total)]

    def families(self):
        return sorted({entry.family for entry in self.entries})


def route_predicate(family, locale):
    """Eskip predicate for one family/locale route, matching the path the entries use."""
    if family == "contentful_spaces":
        return 'PathRegexp("^/spaces/")'
    if family.startswith("shop_"):
        prefix = next(template for name, template, _, _ in SHOP_FAMILIES if name == family)
        prefix = prefix.split("{id}")[0].split("?")[0].replace("{locale}", locale)
        prefix = prefix.rstrip("/")
        return f'PathRegexp("^{prefix}(/|$)")'
    for key, name in CONTENTFUL_FAMILIES.items():
        if name == family and key == "unifyFootersExternal":
            return f'Path("/api/contentful/unify-footers/external/{locale}")'
        if name == family and key == "unifyFootersDirect":
            return f'Path("/api/contentful/unify-footers/{locale}")'
        if name == family and key == "voucherMessages":
            return f'Path("/api/contentful/voucher-messages/{locale}")'
    raise ValueError(f"no route for {family}/{locale}")


def render_routes(entries):
    seen = []
    for entry in entries:
        if (entry.family, entry.locale) not in seen:
            seen.append((entry.family, entry.locale))
    lines = [f'r0: * -> cache({CACHE_ARGS}) -> "{ORIGIN}";']
    for family, locale in seen:
        route_id = f"{family}__{locale}"
        lines.append(f'{route_id}: {route_predicate(family, locale)} -> '
                     f'cache({CACHE_ARGS}) -> "{ORIGIN}";')
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "routes":
        sys.stdout.write(render_routes(build_entries()))
    else:
        raise SystemExit("usage: python3 catalogue.py routes > routes.eskip")
