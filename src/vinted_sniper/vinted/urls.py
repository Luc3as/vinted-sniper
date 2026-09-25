"""Turning a pasted Vinted search URL into something we can poll.

Users paste whatever is in their address bar. That URL carries tracking parameters that
change on every page load, sometimes points at a brand or category page rather than a
search, and always belongs to one country's site. This module reduces all of that to a
canonical URL (used as the uniqueness key for a search) plus the API parameters to send.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Vinted runs one site per country. The domain decides the catalog, the currency and the
# sellers you see, so it travels with the search from bootstrap through to the link in
# your notification.
KNOWN_TLDS: Final[frozenset[str]] = frozenset(
    {
        "fr", "de", "nl", "es", "it", "pl", "be", "at", "cz", "sk", "lt", "pt",
        "se", "ro", "hu", "gr", "fi", "dk", "ie", "lu", "co.uk", "com",
    }
)  # fmt: skip

# Query parameters the site adds for its own bookkeeping. They differ between two visits
# to the same search, so they must not reach the uniqueness key.
_VOLATILE_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "time",
        "search_id",
        "page",
        "per_page",
        "disabled_personalization",
        "referrer",
        "utm_source",
        "utm_medium",
        "utm_campaign",
    }
)

# Browser parameter name -> API parameter name. The site uses PHP-style array syntax in
# the address bar and singular/plural names in the API.
_PARAM_ALIASES: Final[dict[str, str]] = {
    "catalog": "catalog_ids",
    "catalog_ids": "catalog_ids",
    "brand_ids": "brand_ids",
    "brand": "brand_ids",
    "status": "status_ids",
    "status_ids": "status_ids",
    "size_ids": "size_ids",
    "size": "size_ids",
    "color_ids": "color_ids",
    "color": "color_ids",
    "material_ids": "material_ids",
    "country_ids": "country_ids",
    "city_ids": "city_ids",
    "video_game_rating_ids": "video_game_rating_ids",
}

# Parameters that take a single value rather than a list.
_SCALAR_PARAMS: Final[frozenset[str]] = frozenset(
    {"search_text", "price_from", "price_to", "currency", "order", "is_for_swap"}
)

_BRAND_PATH = re.compile(r"^/brand/(\d+)")
_CATALOG_PATH = re.compile(r"^/catalog/(\d+)")


class InvalidSearchURLError(ValueError):
    """The pasted text is not a Vinted search URL we can work with."""


def extract_tld(url: str) -> str:
    """Return the country suffix of a Vinted URL, e.g. 'fr' or 'co.uk'."""
    host = urlparse(url).netloc.lower().split(":")[0]
    host = host.removeprefix("www.")
    if not host.startswith("vinted."):
        raise InvalidSearchURLError(f"{url!r} is not a vinted.* address")

    tld = host.removeprefix("vinted.")
    if tld not in KNOWN_TLDS:
        raise InvalidSearchURLError(
            f"{tld!r} is not a Vinted country site. Known sites: {', '.join(sorted(KNOWN_TLDS))}"
        )
    return tld


def parse_search_params(url: str) -> dict[str, str]:
    """Extract the API parameters a pasted search URL is asking for.

    Category and brand landing pages carry their filter in the path rather than the query
    string, so those are folded in too.
    """
    parsed = urlparse(url)
    collected: dict[str, list[str]] = {}

    if brand := _BRAND_PATH.match(parsed.path):
        collected.setdefault("brand_ids", []).append(brand.group(1))
    if catalog := _CATALOG_PATH.match(parsed.path):
        collected.setdefault("catalog_ids", []).append(catalog.group(1))

    for raw_key, value in parse_qsl(parsed.query, keep_blank_values=False):
        key = raw_key.removesuffix("[]")
        if key in _VOLATILE_PARAMS:
            continue
        if key in _SCALAR_PARAMS:
            collected[key] = [value]
        elif api_key := _PARAM_ALIASES.get(key):
            collected.setdefault(api_key, []).append(value)
        # Anything unrecognised is dropped: passing unknown parameters through has
        # historically produced empty result sets rather than errors.

    params = {key: ",".join(values) for key, values in collected.items() if values}

    # We only ever want what was posted since the last check, newest first.
    params["order"] = "newest_first"
    return params


def normalise_search_url(url: str) -> str:
    """Return a stable canonical URL for a search.

    Two pastes of the same search produce the same string here, which is what lets the
    database reject duplicates instead of polling the same catalog twice.
    """
    url = url.strip()
    if not url:
        raise InvalidSearchURLError("no URL given")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    tld = extract_tld(url)
    params = parse_search_params(url)
    if len(params) == 1:  # only the order we added ourselves
        raise InvalidSearchURLError(
            "that URL has no search filters on it. Open Vinted, set up the search you want, "
            "then copy the address bar once results are showing."
        )

    # safe="+" keeps multi-word searches intact: the site encodes spaces as '+' and
    # re-encoding those to %2B turns "nike air" into a search for a literal plus sign.
    query = urlencode(sorted(params.items()), safe="+,")
    return f"https://www.vinted.{tld}/catalog?{query}"


def build_search_url(tld: str, params: dict[str, str]) -> str:
    """The other direction: a params dict back into a canonical search URL.

    Magic Search produces parameters without a URL ever existing — nobody pasted one — but
    a standing watch is keyed on its canonical URL, so promoting a sweep needs this way
    round. The result is handed straight to `normalise_search_url()` rather than returned
    as built, so a watch created from a sweep and one created from a paste land on exactly
    the same string and the duplicate check between them actually works.

    `safe="+,"` keeps comma-joined id lists (`size_ids=207,208`) and the site's own
    encoding of spaces intact; every key `MappedQuery.to_params()` emits is recognised by
    `parse_search_params()`, so this round-trips.

    Raises `InvalidSearchURLError` for an unknown country site, or for params that carry no
    filter at all — both via the canonicaliser, so the wording matches the paste path.
    """
    query = urlencode(sorted(params.items()), safe="+,")
    return normalise_search_url(f"https://www.vinted.{tld}/catalog?{query}")


def catalog_endpoint(tld: str) -> str:
    """The catalog search service. Vinted retired /api/v2/catalog/items (an HTML 404
    since ~2026-09-16); the frontend now calls svc-catalogue on the api. subdomain,
    with the same response shape but its own filter dialect (`catalog_api_params()`)."""
    return f"https://api.vinted.{tld}/svc-catalogue/items"


# The facets svc-catalogue folds ids under. Stored searches keep the /api/v2 names —
# they round-trip through `parse_search_params()` and `build_search_url()` — and are
# translated at the wire, so nothing saved in the database had to change.
_ATTRIBUTE_FACETS: Final[dict[str, str]] = {
    "catalog_ids": "catalog",
    "brand_ids": "brand",
    "size_ids": "size",
    "status_ids": "status",
    "color_ids": "color",
    "material_ids": "material",
}


def catalog_api_params(params: dict[str, str]) -> dict[str, str]:
    """A stored search's params, said the way svc-catalogue filters.

    The old endpoint filtered on `brand_ids=…&size_ids=…`; svc-catalogue accepts those
    names without complaint and ignores them entirely (verified live 2026-09-16 —
    identical result sets with and without). It filters on `attribute_ids[<facet>]`,
    comma-joined; repeating the key instead is a 400. Scalars — search_text, price_to,
    order and friends — still work under their old names and pass through unchanged.
    """
    translated: dict[str, str] = {}
    for key, value in params.items():
        if facet := _ATTRIBUTE_FACETS.get(key):
            translated[f"attribute_ids[{facet}]"] = value
        else:
            translated[key] = value
    return translated


def dropped_search_words(params: dict[str, str]) -> list[str]:
    """The words `search_request_params()` keeps out of this request, one per token.

    Whoever drops them still owes them to the user: a sweep ranks on them
    (engine/sweep.py:score_title), a standing search gates its alerts on them
    (engine/filters.py). With no structured filter nothing is dropped — alone,
    `search_text` is still a fuzzy hint and travels.
    """
    if any(key in _ATTRIBUTE_FACETS for key in params):
        return (params.get("search_text") or "").split()
    return []


def search_request_params(params: dict[str, str]) -> dict[str, str]:
    """A catalog request's params, with `search_text` kept only when it is the only net.

    The old endpoint took `search_text` as a relevance hint; svc-catalogue filters on it —
    every token has to appear in the title or description (verified live 2026-09-25:
    brand+size+price plus "Patagonia hardshell" answers 1 item where the site's own page,
    which searches through a different backend, says 150). Sent alongside structured
    filters it emptied sweeps and starved standing searches alike, so when a structured
    filter already narrows the read the words stay out of the request and the caller
    applies them itself (`dropped_search_words()`). With no structured filter they stay
    in: unfiltered, svc-catalogue is the whole site.
    """
    if dropped_search_words(params):
        return {key: value for key, value in params.items() if key != "search_text"}
    return dict(params)


def filters_search_endpoint(tld: str) -> str:
    """Search within one filter's options — e.g. brand autocomplete.

    The picker's data moved with the catalog: /api/v2/catalog/filters/* and
    /api/v2/brands all answer 403 since ~2026-09-16, and the site's frontend asks
    the svc-filters service instead. Category scoping travels as
    `attribute_ids[catalog]`, the same dialect as `catalog_api_params()`.
    """
    return f"https://api.vinted.{tld}/svc-filters/filters/search"


def filters_facets_endpoint(tld: str) -> str:
    """The options of one filter (condition, colour, size…), scoped to a category."""
    return f"https://api.vinted.{tld}/svc-filters/filters/facets"


def user_endpoint(tld: str, user_id: int) -> str:
    """One seller's public profile — the reputation numbers the catalog stopped carrying."""
    return f"https://www.vinted.{tld}/api/v2/users/{user_id}"


def wardrobe_endpoint(tld: str, user_id: int) -> str:
    """One seller's public wardrobe — the only anonymous way to tell a live listing
    from a gone one, because sold items simply disappear from it."""
    return f"https://www.vinted.{tld}/api/v2/wardrobe/{user_id}/items"


def catalog_page(tld: str) -> str:
    """The search page itself — its HTML embeds the category tree and the CSRF token."""
    return f"https://www.vinted.{tld}/catalog"


def site_root(tld: str) -> str:
    return f"https://www.vinted.{tld}/"


def swap_tld(url: str, tld: str) -> str:
    """The same URL on another country site. Vinted's catalog, brand, size and status ids
    are shared across its sites, so nothing but the domain changes."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return urlunparse(parsed._replace(netloc=f"www.vinted.{tld}"))


def item_url(tld: str, item_id: int) -> str:
    return f"https://www.vinted.{tld}/items/{item_id}"


def member_url(tld: str, user_id: int) -> str:
    """A seller's profile page. The site accepts the bare id; no login slug needed."""
    return f"https://www.vinted.{tld}/member/{user_id}"


def message_seller_url(tld: str, item_id: int) -> str:
    """Where to go to ask the seller about a listing.

    Vinted's web app used to expose `/items/{id}/want_it/new` for this; every notifier on
    GitHub still links to it, and it has answered "page not found" since the front end was
    rebuilt. The message screen is now reached from the listing page, which is where this
    sends you — the "Ask seller" button sits right under the price.
    """
    return item_url(tld, item_id)


def buy_url(tld: str, item_id: int) -> str:
    """Where to go to buy a listing.

    `/transaction/buy/new?transaction[item_id]=…` is gone too: checkout now needs a
    transaction the site creates when you press "Buy now", so there is no address that
    opens it directly. The listing page is the closest working thing. Nothing here buys
    anything on anyone's behalf.
    """
    return item_url(tld, item_id)
