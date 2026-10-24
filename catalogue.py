#!/usr/bin/env python3
"""Endpoint catalogue for the cache workload, and the Skipper routes that serve it.

Contentful families come from contentful-cache-paths.json, a snapshot of production request
counts (Dash0, 6h window). Each path keeps its real count as its traffic weight. The mock
online-shop endpoints are cacheable storefront GETs generated for every Contentful locale.
They take SHOP_SHARE of total traffic, split by the "shop" pool shares in FAMILIES.

Each family and locale gets its own Skipper route, so per-route metrics carry both labels:
route id "<family>__<locale>". `python3 catalogue.py routes > routes.eskip` renders the routes.

Beyond the storefront, the extra families cover common caching cases: hashed static assets
(with ETag revalidation and HEAD), image resizing, personalised paths that must not be cached,
POST cart mutations, and robots/sitemap. Identifiers with ``?case=`` select an origin scenario
(see ORIGIN_CASES), which sets the status or Cache-Control the origin answers with.
"""
import bisect
import json
import sys
from collections import namedtuple
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent / "contentful-cache-paths.json"
ORIGIN = "http://127.0.0.1:8081"
CACHE_ARGS = '"30s", "10s", "15s"'
SHOP_SHARE = 0.25
# Share of total traffic on the extra families (see FAMILIES, pool "extra").
EXTRA_SHARE = 0.15
# Origin scenarios selected by ?case=; lifecycle.py posts these to the mock origin's control API.
# Cases not listed here keep the origin's default behaviour (200, public max-age=2).
ORIGIN_CASES = {
    "notfound": {"status": 404},
    "upstream503": {"status": 503},
    "private": {"cache_control": "private, max-age=60"},
    "nostore": {"cache_control": "private, no-store"},
}
# Families whose responses carry an ETag; the workload revalidates them with If-None-Match.
CONDITIONAL_FAMILIES = {"static_assets"}
# Contentful paths the snapshot marks as not cache-backed (upstream 500s).
EXCLUDED_PATHS = {"/api/contentful/unify-footers/external/en_FI"}
CONTENTFUL_FAMILIES = {
    "spaces": "contentful_spaces",
    "unifyFootersDirect": "contentful_footers_direct",
    "unifyFootersExternal": "contentful_footers_external",
    "voucherMessages": "contentful_vouchers",
}
# One Family per Skipper route shape. The predicate is eskip; {locale} is substituted per locale.
# pool "shop" takes SHOP_SHARE of traffic and pool "extra" takes EXTRA_SHARE; share is the
# family's fraction of its pool. scope "locale" repeats per footer locale, "global" uses "all".
# Each identifier is one entry and splits the family's weight evenly.
Family = namedtuple("Family", "name pool scope share template predicate method ids")
STATIC_IDS = ("js/app.3f2a1c.js", "css/main.9b8e7d.css", "img/hero.4c1d2e.webp",
              "fonts/inter.7a6b5c.woff2")
FAMILIES = (
    Family("shop_products", "shop", "locale", 0.40, "/shop/{locale}/api/products/{id}",
           'PathRegexp("^/shop/{locale}/api/products(/|$)")', "GET",
           ("sku-1001", "sku-1002", "sku-1003", "sku-1004", "sku-1005", "sku-1006", "sku-1007",
            "sku-1008", "sku-missing?case=notfound", "sku-broken?case=upstream503")),
    Family("shop_categories", "shop", "locale", 0.20, "/shop/{locale}/api/categories/{id}",
           'PathRegexp("^/shop/{locale}/api/categories(/|$)")', "GET",
           ("women-shoes", "men-clothing", "sale", "new-in",
            "women-shoes?sort=price_asc", "women-shoes?sort=price_asc&page=2",
            "men-clothing?page=3", "sale?size=M")),
    Family("shop_search", "shop", "locale", 0.15, "/shop/{locale}/api/search?q={id}",
           'PathRegexp("^/shop/{locale}/api/search$")', "GET",
           ("sneakers", "jacket", "dress")),
    Family("shop_navigation", "shop", "locale", 0.15, "/shop/{locale}/api/navigation",
           'PathRegexp("^/shop/{locale}/api/navigation(/|$)")', "GET", ("main",)),
    Family("shop_promotions", "shop", "locale", 0.10, "/shop/{locale}/api/promotions/banner",
           'PathRegexp("^/shop/{locale}/api/promotions(/|$)")', "GET", ("banner",)),
    Family("shop_personal", "extra", "locale", 0.15, "/shop/{locale}/api/{id}",
           'PathRegexp("^/shop/{locale}/api/(cart$|account/)")', "GET",
           ("cart?case=private", "account/summary?case=nostore")),
    Family("shop_mutations", "extra", "locale", 0.10, "/shop/{locale}/api/{id}",
           'Method("POST") && PathRegexp("^/shop/{locale}/api/cart/items$")', "POST",
           ("cart/items",)),
    Family("static_assets", "extra", "global", 0.40, "/static/{id}",
           'PathRegexp("^/static/")', "GET", STATIC_IDS),
    Family("static_head", "extra", "global", 0.05, "/static/{id}",
           'Method("HEAD") && PathRegexp("^/static/")', "HEAD", STATIC_IDS),
    Family("shop_images", "extra", "global", 0.20, "/shop/images/resize/{id}",
           'PathRegexp("^/shop/images/")', "GET",
           ("sku-1001.jpg?w=400&q=80", "sku-1002.jpg?w=800&q=70", "banner.webp?w=1600")),
    Family("site_meta", "extra", "global", 0.10, "/{id}",
           'PathRegexp("^/(robots[.]txt|sitemap[.]xml)$")', "GET", ("robots.txt", "sitemap.xml")),
)
SHOP_FAMILY_NAMES = {f.name for f in FAMILIES if f.pool == "shop"}
EXTRA_FAMILY_NAMES = {f.name for f in FAMILIES if f.pool == "extra"}
FAMILY_BY_NAME = {f.name: f for f in FAMILIES}


class Entry:
    __slots__ = ("family", "locale", "path", "weight", "method")

    def __init__(self, family, locale, path, weight, method="GET"):
        self.family = family
        self.locale = locale
        self.path = path
        self.weight = weight
        self.method = method

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
    total = contentful_total / (1 - SHOP_SHARE - EXTRA_SHARE)
    pool_total = {"shop": total * SHOP_SHARE, "extra": total * EXTRA_SHARE}
    footers = families["unifyFootersDirect"]
    footers_total = sum(item["requests"] for item in footers)
    locales = [(_locale(item["path"]), item["requests"] / footers_total) for item in footers]
    for family in FAMILIES:
        scopes = locales if family.scope == "locale" else [("all", 1.0)]
        for locale, locale_share in scopes:
            for identifier in family.ids:
                weight = pool_total[family.pool] * family.share * locale_share / len(family.ids)
                path = family.template.format(locale=locale, id=identifier)
                method = family.method
                entries.append(Entry(family.name, locale, path, weight, method))
    return entries


class Catalogue:
    """Weighted picker over all entries; pick(rng) returns one Entry."""

    def __init__(self, entries):
        self.entries = entries
        self.set_boost(frozenset(), 1)

    def set_boost(self, families, factor):
        """Multiply the weight of every entry in families by factor (1 restores base weights)."""
        self.cumulative = []
        total = 0.0
        for entry in self.entries:
            total += entry.weight * (factor if entry.family in families else 1)
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
    if family in FAMILY_BY_NAME:
        return FAMILY_BY_NAME[family].predicate.replace("{locale}", locale)
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
