"""Synthetic retail origin content for the --retail mock origin. Standard library only.

Every response is a pure function of (family, path, sorted query, version). The seed is the
sha256 of the path and sorted query (plus the version when it is not "A"), so the same URL
always yields identical bytes, ETag and headers, as a production origin would.

Only JSON is generated, matching what Contentful and the shop API send. Provenance: the
Contentful path shapes (spaces, unify-footers, voucher-messages) come from measured traffic in
contentful-cache-paths.json. Shop families and the spaces ``?size=2mb`` variant are synthetic
ASSUMPTIONS chosen to exercise cache behaviour; their sizes are not production data.
"""

import hashlib
import json
import random
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

LOCALES = {
    "cs_CZ": "CZK", "da_DK": "DKK", "de_AT": "EUR", "de_CH": "CHF", "de_DE": "EUR",
    "de_LU": "EUR", "en_FI": "EUR", "en_GB": "GBP", "es_ES": "EUR", "et_EE": "EUR",
    "fi_FI": "EUR", "fr_BE": "EUR", "fr_CH": "CHF", "fr_FR": "EUR", "fr_LU": "EUR",
    "hr_HR": "EUR", "hu_HU": "HUF", "it_IT": "EUR", "lt_LT": "EUR", "lv_LV": "EUR",
    "nl_BE": "EUR", "nl_NL": "EUR", "no_NO": "NOK", "pl_PL": "PLN", "ro_RO": "RON",
    "sk_SK": "EUR", "sl_SI": "EUR", "sv_SE": "SEK",
}
LAST_MODIFIED = "Wed, 01 Jan 2025 00:00:00 GMT"
MIB = 1024 * 1024
# Shared space id for footers and vouchers (assumption; spaces paths carry their own id).
CONTENTFUL_SPACE = "8397t4ds1z4f"
PLACEHOLDER = 99999  # widest count width, so the packed envelope is never shorter than the final one

# Target body sizes in bytes (inclusive); a body lands exactly on its drawn target.
FAMILY_SIZES = {
    "contentful_spaces": (20_000, 60_000),
    "contentful_footers_direct": (3_000, 8_000),
    "contentful_footers_external": (3_000, 8_000),
    "contentful_vouchers": (3_000, 6_000),
    "shop_products": (2_000, 20_000),
    "shop_categories": (20_000, 200_000),
    "shop_search": (10_000, 100_000),
    "shop_navigation": (5_000, 30_000),
    "shop_promotions": (1_000, 5_000),
    "shop_personal": (1_000, 10_000),
    "shop_mutations": (200, 2_000),
}
CACHE_CONTROL = {
    "contentful_spaces": "public, max-age=120, stale-while-revalidate=30, stale-if-error=300",
    "contentful_footers_direct": "public, max-age=300, stale-while-revalidate=60, stale-if-error=600",
    "contentful_footers_external": "public, max-age=300, stale-while-revalidate=60, stale-if-error=600",
    "contentful_vouchers": "public, max-age=60, stale-while-revalidate=30, stale-if-error=300",
    "shop_products": "public, max-age=300, stale-while-revalidate=60, stale-if-error=600",
    "shop_categories": "public, max-age=120, stale-while-revalidate=30, stale-if-error=300",
    "shop_search": "public, max-age=60, stale-while-revalidate=30, stale-if-error=120",
    "shop_navigation": "public, max-age=600, stale-while-revalidate=120, stale-if-error=1200",
    "shop_promotions": "public, max-age=60, stale-while-revalidate=30, stale-if-error=300",
    "shop_personal": "private, max-age=30",
    "shop_mutations": "no-store",
    "negative": "no-store",
}
CATEGORY_SLUGS = ("women-shoes", "men-clothing", "sale", "new-in")
SKUS = tuple(f"sku-{n}" for n in range(1001, 1009)) + ("sku-missing", "sku-broken")
ADJECTIVES = ("classic", "soft", "lightweight", "relaxed", "tailored", "breathable",
              "everyday", "rugged", "slim", "cosy", "bold", "minimal")
MATERIALS = ("leather", "canvas", "wool", "cotton", "suede", "recycled nylon", "linen",
             "denim", "knit")
NOUNS = ("boot", "jacket", "dress", "sneaker", "shirt", "coat", "loafer", "sandal",
         "cardigan", "trouser")
DETAILS = ("stitched", "lined", "machine washable", "water resistant",
           "with a cushioned insole", "with a removable insole", "made in Europe",
           "ethically sourced", "with reinforced toe", "featuring a zip closure",
           "with a relaxed fit", "designed for daily wear")
WORDS = ADJECTIVES + MATERIALS + NOUNS + DETAILS
COLOURS = ("black", "navy", "sand", "olive", "burgundy", "oatmeal", "charcoal")
SIZES = ("36", "37", "38", "39", "40", "41", "42", "43", "44")


@dataclass(frozen=True)
class Endpoint:
    origin: str
    path: str
    family: str
    locale: str


def _locale_of(path):
    return next((part for part in path.split("/") if part in LOCALES), "")


def _space_id(path):
    parts = path.split("/")
    return parts[parts.index("spaces") + 1]


def _dumps(obj):
    return json.dumps(obj)


def _stamp(rng):
    return (f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
            f"T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00.000Z")


def _text(rng, n):
    """Exactly n ASCII characters of realistic phrase text (no JSON escaping needed)."""
    if n <= 0:
        return ""
    parts, length = [], 0
    while length <= n:
        word = rng.choice(WORDS)
        parts.append(word)
        length += len(word) + 1
    return " ".join(parts)[:n]


def _name(rng):
    return f"{rng.choice(ADJECTIVES)} {rng.choice(MATERIALS)} {rng.choice(NOUNS)}".title()


def _amount(rng):
    return f"{rng.randint(4, 399)}.{rng.choice(('00', '49', '90', '95', '99'))}"


def _sys(rng, kind, locale):
    return {"id": f"{kind}-{rng.randrange(10**6, 10**7)}", "type": "Entry",
            "contentType": kind, "locale": locale or None,
            "createdAt": _stamp(rng), "updatedAt": _stamp(rng)}


def _pagination(rng, total_max):
    return {"skip": 0, "limit": 0, "total": rng.randint(1, total_max)}


def _json_listing(rng, target, head, make_item, first=None):
    """Greedy whole items, then exact padding into the last item's description.

    While packing, the pagination limit holds a 4-digit placeholder; it is replaced by the real
    item count before the padding is measured, so the final length is exactly `target`.
    """
    if "pagination" in head:
        head["pagination"]["limit"] = 9999
    items = []
    total = len(_dumps({**head, "items": []}))
    if first is not None:
        items.append(first)
        total += len(_dumps(first))
    while True:
        item = make_item(rng, len(items))
        cost = len(_dumps(item)) + (2 if items else 0)
        if total + cost > target:
            break
        items.append(item)
        total += cost
    if not items:
        raise ValueError(f"target {target} too small for one item")
    head["items"] = items
    if "pagination" in head:
        head["pagination"]["limit"] = len(items)
    pad = target - len(_dumps(head))
    if pad < 0:
        raise ValueError(f"target {target} too small for the envelope")
    items[-1]["fields"]["description"] += _text(rng, pad)
    return _dumps(head).encode()


def _listing(family, rng, head, make_item, first=None):
    body = _json_listing(rng, rng.randint(*FAMILY_SIZES[family]), head, make_item, first)
    return 200, "application/json", body


def _cf_locale(locale):
    """Contentful locale code: en_GB -> en-GB. Empty means the default en-GB."""
    return (locale or "en_GB").replace("_", "-")


def _cf_sys(sys_id, kind, space, cl, rng, content_type=None):
    sys = {"id": sys_id, "type": kind,
           "space": {"sys": {"id": space, "type": "Link", "linkType": "Space"}},
           "environment": {"sys": {"id": "master", "type": "Link", "linkType": "Environment"}},
           "locale": cl, "revision": rng.randint(1, 40),
           "createdAt": _stamp(rng), "updatedAt": _stamp(rng)}
    if content_type:
        sys["contentType"] = {"sys": {"id": content_type, "type": "Link", "linkType": "ContentType"}}
    return sys


def _link(link_type, target_id):
    return {"sys": {"id": target_id, "type": "Link", "linkType": link_type}}


def _asset(asset_id, space, cl):
    """Asset metadata only: the file URL is on a CDN host this mock never serves."""
    rng = random.Random(asset_id)
    name = _name(rng)
    slug = name.lower().replace(" ", "-")
    digest = hashlib.sha256(asset_id.encode()).hexdigest()[:16]
    file_info = {"url": f"//images.ctfassets.net/{space}/{asset_id}/{digest}/{slug}.jpg",
                 "fileName": f"{slug}.jpg", "contentType": "image/jpeg",
                 "details": {"size": rng.randint(40_000, 400_000),
                             "image": {"width": 1200, "height": 800}}}
    return {"metadata": {"tags": []}, "sys": _cf_sys(asset_id, "Asset", space, cl, rng),
            "fields": {"title": {cl: name}, "file": {cl: file_info}}}


def _linked_entry(entry_id, space, cl):
    rng = random.Random(entry_id)
    return {"metadata": {"tags": []},
            "sys": _cf_sys(entry_id, "Entry", space, cl, rng, content_type="linkedPage"),
            "fields": {"title": {cl: _name(rng)},
                       "description": {cl: _text(rng, rng.randint(40, 120))}}}


def _cf_entry(rng, kind, space, cl):
    fields = {"title": {cl: _name(rng)},
              "slug": {cl: f"{rng.choice(ADJECTIVES)}-{rng.randrange(10**3, 10**4)}"},
              "heroImage": {cl: _link("Asset", f"asset-{rng.randrange(10**6, 10**7)}")},
              "links": {cl: [_link("Entry", f"linked-{rng.randrange(10**6, 10**7)}")
                             for _ in range(rng.randint(0, 2))]},
              "description": {cl: _text(rng, rng.randint(40, 160))}}
    return {"metadata": {"tags": []},
            "sys": _cf_sys(f"{kind}-{rng.randrange(10**6, 10**7)}", "Entry", space, cl, rng, kind),
            "fields": fields}


def _cf_includes(item, space, cl, includes):
    """Add the Asset and linked Entry objects an item references, keyed by id (deduplicated)."""
    hero = item["fields"]["heroImage"][cl]["sys"]["id"]
    includes["Asset"].setdefault(hero, _asset(hero, space, cl))
    for link in item["fields"]["links"][cl]:
        target = link["sys"]["id"]
        includes["Entry"].setdefault(target, _linked_entry(target, space, cl))


def _cf_envelope(items, includes, total, limit):
    """Contentful collection shape: sys, total, skip, limit, items, includes."""
    return {"sys": {"type": "Array"}, "total": total, "skip": 0, "limit": limit, "items": items,
            "includes": {"Asset": list(includes["Asset"].values()),
                         "Entry": list(includes["Entry"].values())}}


def _seal(rng, items, includes, target, cl, extra=None):
    """Set the real count fields, then grow the last item's description to exactly target bytes."""
    envelope = _cf_envelope(items, includes, len(items) + rng.randint(0, 40), len(items))
    if extra:
        envelope = {"synthetic": extra, **envelope}
    pad = target - len(_dumps(envelope))
    if pad < 0:
        raise ValueError(f"target {target} too small for the envelope")
    description = items[-1]["fields"]["description"]
    description[cl] += _text(rng, pad)
    return _dumps(envelope).encode()


def _contentful_listing(family, rng, space, cl, make_item):
    """Greedy whole entries up to a drawn target, then exact padding into the last entry."""
    target = rng.randint(*FAMILY_SIZES[family])
    items, includes = [], {"Asset": {}, "Entry": {}}
    while True:
        candidate = make_item(rng, len(items))
        trial = {"Asset": dict(includes["Asset"]), "Entry": dict(includes["Entry"])}
        _cf_includes(candidate, space, cl, trial)
        if len(_dumps(_cf_envelope(items + [candidate], trial, PLACEHOLDER, PLACEHOLDER))) > target:
            break
        items.append(candidate)
        includes = trial
    if not items:
        raise ValueError(f"target {target} too small for one entry")
    return 200, "application/json", _seal(rng, items, includes, target, cl)


def _card(rng, kind, locale, currency):
    sku = f"sku-{rng.randint(1000, 9999)}"
    return {"sys": _sys(rng, kind, locale),
            "fields": {"sku": sku, "name": _name(rng),
                       "price": {"amount": _amount(rng), "currency": currency},
                       "image": {"url": f"/shop/images/resize/{sku}.jpg?w=400&q=80"},
                       "description": _text(rng, rng.randint(60, 200))}}


def _variant(rng, locale, currency):
    return {"sys": _sys(rng, "productVariant", locale),
            "fields": {"colour": rng.choice(COLOURS), "size": rng.choice(SIZES),
                       "ean": "".join(str(rng.randrange(10)) for _ in range(13)),
                       "stock": rng.randint(0, 60),
                       "price": {"amount": _amount(rng), "currency": currency},
                       "description": _text(rng, rng.randint(40, 140))}}


def _nav_node(rng, locale):
    slug = rng.choice(CATEGORY_SLUGS)
    return {"sys": _sys(rng, "navigationNode", locale),
            "fields": {"label": _name(rng).split()[-1],
                       "url": f"/shop/{locale}/api/categories/{slug}",
                       "links": [{"sys": {"id": f"node-{rng.randrange(10**4, 10**5)}",
                                          "type": "Link", "linkType": "Entry"}}
                                 for _ in range(rng.randint(3, 8))],
                       "description": _text(rng, rng.randint(20, 80))}}


def _banner(rng, locale):
    return {"sys": _sys(rng, "banner", locale),
            "fields": {"title": _name(rng), "cta": "Shop now",
                       "url": f"/shop/{locale}/api/categories/sale",
                       "image": {"url": f"/shop/images/resize/banner-{locale}.webp?w=1600"},
                       "description": _text(rng, rng.randint(40, 120))}}


def _cart_line(rng, currency):
    return {"sys": {"id": f"line-{rng.randrange(10**4, 10**5)}", "type": "CartLine"},
            "fields": {"sku": f"sku-{rng.randint(1001, 1008)}", "qty": rng.randint(1, 3),
                       "description": _text(rng, rng.randint(20, 80))}}


def _query_value(pairs, key, default):
    return dict(pairs).get(key, default)


def _oversized_spaces(rng, space, cl):
    """SYNTHETIC 2 MB variant, selected by ?size=2mb: one oversized entry padded to exactly 2 MiB.

    Labelled in the body (top-level `synthetic`) and in the entry title. Test fixture only.
    """
    entry = _cf_entry(rng, "blogPost", space, cl)
    entry["fields"]["title"][cl] = "SYNTHETIC 2MB OVERSIZED ENTRY"
    entry["fields"]["description"][cl] = ""
    includes = {"Asset": {}, "Entry": {}}
    _cf_includes(entry, space, cl, includes)
    label = {"variant": "oversized-2mb",
             "label": "SYNTHETIC 2MB VARIANT: oversized-entry test fixture, not production data"}
    return 200, "application/json", _seal(rng, [entry], includes, 2 * MIB, cl, extra=label)


def _build_spaces(rng, path, locale, pairs):
    space = _space_id(path)
    cl = _cf_locale(locale)
    if dict(pairs).get("size") == "2mb":
        return _oversized_spaces(rng, space, cl)
    return _contentful_listing("contentful_spaces", rng, space, cl,
                               lambda r, i: _cf_entry(r, "blogPost", space, cl))


def _build_footers(rng, path, locale, pairs):
    family = "contentful_footers_external" if "/external/" in path else "contentful_footers_direct"
    cl = _cf_locale(locale)
    return _contentful_listing(family, rng, CONTENTFUL_SPACE, cl,
                               lambda r, i: _cf_entry(r, "footerLinkGroup", CONTENTFUL_SPACE, cl))


def _build_vouchers(rng, path, locale, pairs):
    cl = _cf_locale(locale)
    return _contentful_listing("contentful_vouchers", rng, CONTENTFUL_SPACE, cl,
                               lambda r, i: _cf_entry(r, "voucherMessage", CONTENTFUL_SPACE, cl))


def _build_products(rng, path, locale, pairs):
    sku = path.rsplit("/", 1)[-1]
    currency = LOCALES[locale]
    head = {"sys": {"id": sku, "type": "Array", "locale": locale},
            "fields": {"name": _name(rng), "slug": sku, "currency": currency,
                       "description": _text(rng, rng.randint(160, 320)),
                       "images": [{"url": f"/shop/images/resize/{sku}.jpg?w=400&q=80"}]},
            "pagination": _pagination(rng, 12)}
    return _listing("shop_products", rng, head, lambda r, i: _variant(r, locale, currency))


def _build_categories(rng, path, locale, pairs):
    slug = path.rsplit("/", 1)[-1]
    currency = LOCALES[locale]
    head = {"sys": {"id": slug, "type": "Array", "locale": locale},
            "fields": {"title": _name(rng), "slug": slug, "description": _text(rng, 200)},
            "pagination": _pagination(rng, 900)}
    return _listing("shop_categories", rng, head, lambda r, i: _card(r, "productCard", locale, currency))


def _build_search(rng, path, locale, pairs):
    currency = LOCALES[locale]
    query = re.sub(r"[^A-Za-z0-9 -]", "", _query_value(pairs, "q", ""))[:64]
    head = {"sys": {"id": "search", "type": "Array", "locale": locale},
            "fields": {"query": query, "sort": "relevance"},
            "pagination": _pagination(rng, 2000)}
    return _listing("shop_search", rng, head, lambda r, i: _card(r, "searchHit", locale, currency))


def _build_navigation(rng, path, locale, pairs):
    head = {"sys": {"id": "main", "type": "Array", "locale": locale},
            "fields": {"menu": "main"}, "pagination": _pagination(rng, 40)}
    return _listing("shop_navigation", rng, head, lambda r, i: _nav_node(r, locale))


def _build_promotions(rng, path, locale, pairs):
    head = {"sys": {"id": "banner", "type": "Array", "locale": locale},
            "fields": {"placement": "banner"}, "pagination": _pagination(rng, 10)}
    return _listing("shop_promotions", rng, head, lambda r, i: _banner(r, locale))


def _build_personal(rng, path, locale, pairs):
    currency = LOCALES[locale]
    kind = "cart" if path.endswith("/cart") else "account"
    head = {"sys": {"id": kind, "type": "Array", "locale": locale},
            "fields": {"currency": currency, "customer": "anonymous"},
            "pagination": _pagination(rng, 30)}
    return _listing("shop_personal", rng, head, lambda r, i: _cart_line(r, currency))


def _build_mutation(rng, path, locale, pairs):
    """Echo of the requested line (sku/qty from the query string) plus a seeded cart."""
    currency = LOCALES[locale]
    sku = _query_value(pairs, "sku", "sku-1001")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", sku):
        sku = "sku-1001"
    qty = _query_value(pairs, "qty", "1")
    qty = int(qty) if qty.isdigit() and 1 <= int(qty) <= 99 else 1
    head = {"cartId": "cart-" + "".join(rng.choice("0123456789abcdef") for _ in range(12)),
            "items": []}
    first = {"sys": {"id": f"line-{sku}", "type": "CartLine"},
             "fields": {"sku": sku, "qty": qty, "description": "Added to your bag."}}
    return _listing("shop_mutations", rng, head, lambda r, i: _cart_line(r, currency), first=first)


def _build_negative(rng, path, locale, pairs):
    return 500, "application/json", b'{"error":"synthetic upstream failure"}'


BUILDERS = {
    "contentful_spaces": _build_spaces,
    "contentful_footers_direct": _build_footers,
    "contentful_footers_external": _build_footers,
    "contentful_vouchers": _build_vouchers,
    "shop_products": _build_products,
    "shop_categories": _build_categories,
    "shop_search": _build_search,
    "shop_navigation": _build_navigation,
    "shop_promotions": _build_promotions,
    "shop_personal": _build_personal,
    "shop_mutations": _build_mutation,
    "negative": _build_negative,
}


def content(family, path, query="", version="A"):
    """Return (status, headers, body_bytes) for one synthetic origin response."""
    if family not in BUILDERS:
        raise KeyError(f"unknown retail family {family!r}")
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    material = f"{path}?{urlencode(pairs)}"
    if version != "A":
        material += f"|version={version}"
    rng = random.Random(int.from_bytes(hashlib.sha256(material.encode()).digest(), "big"))
    locale = _locale_of(path)
    status, ctype, body = BUILDERS[family](rng, path, locale, pairs)
    headers = {
        "Content-Type": ctype,
        "Cache-Control": CACHE_CONTROL[family],
        "ETag": f'"{hashlib.sha256(body).hexdigest()}"',
        "Last-Modified": LAST_MODIFIED,
    }
    if locale:
        headers["Vary"] = "Accept-Language"
    return status, headers, body


def _endpoints():
    endpoints = {}

    def add(origin, path, family):
        endpoints[(origin, path)] = Endpoint(origin, path, family, _locale_of(path))

    for locale in LOCALES:
        for sku in SKUS:
            add("gateway", f"/shop/{locale}/api/products/{sku}", "shop_products")
        for slug in CATEGORY_SLUGS:
            add("gateway", f"/shop/{locale}/api/categories/{slug}", "shop_categories")
        add("gateway", f"/shop/{locale}/api/search", "shop_search")
        add("gateway", f"/shop/{locale}/api/navigation", "shop_navigation")
        add("gateway", f"/shop/{locale}/api/promotions/banner", "shop_promotions")
        add("gateway", f"/shop/{locale}/api/cart", "shop_personal")
        add("gateway", f"/shop/{locale}/api/account/summary", "shop_personal")
        add("gateway", f"/shop/{locale}/api/cart/items", "shop_mutations")
        add("cms", f"/api/contentful/unify-footers/{locale}", "contentful_footers_direct")
        add("cms", f"/api/contentful/unify-footers/external/{locale}", "contentful_footers_external")
        add("cms", f"/api/contentful/voucher-messages/{locale}", "contentful_vouchers")
    add("cms", "/spaces/8397t4ds1z4f/environments/master/entries", "contentful_spaces")
    add("cms", "/spaces/gscn32h7ckds/environments/master/entries", "contentful_spaces")
    add("gateway", "/shop/api/negative-fixture", "negative")
    return endpoints


ORIGIN_ENDPOINTS = _endpoints()
