from __future__ import annotations

import pytest

from vinted_sniper.vinted import urls


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.vinted.fr/catalog?search_text=x", "fr"),
        ("https://vinted.de/catalog?search_text=x", "de"),
        ("https://www.vinted.co.uk/catalog?search_text=x", "co.uk"),
        ("https://www.vinted.com/catalog?search_text=x", "com"),
    ],
)
def test_country_site_comes_from_the_domain(url: str, expected: str) -> None:
    assert urls.extract_tld(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ebay.fr/catalog?search_text=x",
        "https://www.vinted.zz/catalog?search_text=x",
    ],
)
def test_non_vinted_addresses_are_refused(url: str) -> None:
    with pytest.raises(urls.InvalidSearchURLError):
        urls.extract_tld(url)


def test_array_parameters_become_api_names() -> None:
    params = urls.parse_search_params(
        "https://www.vinted.fr/catalog?catalog[]=1904&brand_ids[]=53&status[]=6&status[]=1"
    )

    assert params["catalog_ids"] == "1904"
    assert params["brand_ids"] == "53"
    assert params["status_ids"] == "6,1", "repeated filters should combine, not overwrite"


def test_category_and_brand_landing_pages_carry_their_filter_in_the_path() -> None:
    assert urls.parse_search_params("https://www.vinted.fr/catalog/1904-femmes")["catalog_ids"] == (
        "1904"
    )
    assert urls.parse_search_params("https://www.vinted.fr/brand/53-nike")["brand_ids"] == "53"


def test_tracking_parameters_are_dropped() -> None:
    params = urls.parse_search_params(
        "https://www.vinted.fr/catalog?search_text=nike&time=1700000000"
        "&search_id=12345&page=3&disabled_personalization=true&utm_source=x"
    )

    assert set(params) == {"search_text", "order"}


def test_results_are_always_requested_newest_first() -> None:
    params = urls.parse_search_params("https://www.vinted.fr/catalog?search_text=x&order=relevance")

    assert params["order"] == "newest_first"


def test_same_search_pasted_twice_normalises_to_one_key() -> None:
    first = urls.normalise_search_url(
        "https://www.vinted.fr/catalog?search_text=nike&price_to=30&time=111&search_id=a"
    )
    second = urls.normalise_search_url(
        "https://www.vinted.fr/catalog?price_to=30&search_text=nike&time=222&search_id=b&page=2"
    )

    assert first == second


def test_multi_word_searches_keep_their_plus_signs() -> None:
    normalised = urls.normalise_search_url("https://www.vinted.fr/catalog?search_text=nike+air+max")

    assert "search_text=nike+air+max" in normalised
    assert "%2B" not in normalised, "a re-encoded plus turns the search into a literal '+'"


def test_url_without_filters_is_refused_with_advice() -> None:
    with pytest.raises(urls.InvalidSearchURLError, match="no search filters"):
        urls.normalise_search_url("https://www.vinted.fr/catalog")


def test_missing_scheme_is_tolerated() -> None:
    assert urls.normalise_search_url("www.vinted.fr/catalog?search_text=x").startswith("https://")


def test_deep_links_stay_on_the_search_country_site_and_resolve() -> None:
    assert urls.item_url("de", 42) == "https://www.vinted.de/items/42"
    # The old want_it/new and transaction/buy/new routes are gone from Vinted's front end;
    # anything we link must be a page that actually loads.
    assert urls.message_seller_url("de", 42) == "https://www.vinted.de/items/42"
    assert urls.buy_url("de", 42) == "https://www.vinted.de/items/42"
    assert urls.member_url("de", 7) == "https://www.vinted.de/member/7"


# --- Params back into a URL -----------------------------------------------------------
#
# The direction Magic Search needs: it produces parameters without anyone ever pasting a
# URL, but a standing watch is keyed on its canonical URL. What matters is that the two
# directions agree, so a swept watch and a pasted one collide on duplicates.


def test_params_round_trip_through_a_built_url() -> None:
    params = {
        "catalog_ids": "1206",
        "brand_ids": "7",
        "size_ids": "207,208,209",
        "price_to": "120",
        "currency": "EUR",
        "search_text": "torrentshell",
        "order": "newest_first",
    }

    built = urls.build_search_url("sk", params)

    assert urls.parse_search_params(built) == params
    # Comma-joined id lists must survive as commas: %2C would be read back as one id.
    assert "size_ids=207,208,209" in built


def test_a_built_url_is_already_canonical() -> None:
    """Built once and pasted back gives the same string, which is what dedup relies on."""
    built = urls.build_search_url("fr", {"search_text": "nike air", "price_to": "30"})

    assert urls.normalise_search_url(built) == built
    assert built.startswith("https://www.vinted.fr/catalog?")
    assert "%2B" not in built, "a re-encoded plus turns the search into a literal '+'"


def test_a_built_url_still_asks_for_the_newest_first() -> None:
    """The poller's dedup reads 'what appeared since last time', so the order is pinned."""
    built = urls.build_search_url("de", {"catalog_ids": "1206"})

    assert "order=newest_first" in built
    assert urls.parse_search_params(built)["order"] == "newest_first"


def test_building_for_an_unknown_country_site_is_refused() -> None:
    with pytest.raises(urls.InvalidSearchURLError, match="not a Vinted country site"):
        urls.build_search_url("xx", {"search_text": "nike"})


def test_building_from_nothing_but_an_order_is_refused() -> None:
    with pytest.raises(urls.InvalidSearchURLError, match="no search filters"):
        urls.build_search_url("fr", {"order": "newest_first"})


def test_the_catalog_endpoint_is_the_svc_catalogue_service() -> None:
    """Vinted retired /api/v2/catalog/items (HTML 404 since ~2026-09-16); the site's own
    frontend now calls the svc-catalogue service on the api. subdomain."""
    assert urls.catalog_endpoint("sk") == "https://api.vinted.sk/svc-catalogue/items"
    assert urls.catalog_endpoint("fr") == "https://api.vinted.fr/svc-catalogue/items"


def test_the_filter_endpoints_are_the_svc_filters_service() -> None:
    """The picker's data moved with the catalog: /api/v2/catalog/filters/* and
    /api/v2/brands all answer 403 since ~2026-09-16, and the site's frontend now asks
    the svc-filters service on the api. subdomain instead."""
    assert urls.filters_facets_endpoint("sk") == "https://api.vinted.sk/svc-filters/filters/facets"
    assert urls.filters_search_endpoint("fr") == "https://api.vinted.fr/svc-filters/filters/search"


def test_filter_ids_are_folded_into_attribute_ids_for_svc_catalogue() -> None:
    """svc-catalogue accepts the old /api/v2 filter names without error and ignores them
    entirely (verified live 2026-09-16: identical result sets with and without
    brand_ids). What it filters on is `attribute_ids[<facet>]`, comma-joined — repeating
    the key is a 400 — so the wire params must speak that dialect."""
    params = {
        "search_text": "mikina",
        "order": "newest_first",
        "price_to": "80",
        "brand_ids": "209084,563992",
        "size_ids": "208,209",
        "status_ids": "2,1,6",
        "catalog_ids": "2050",
        "color_ids": "22",
        "material_ids": "44",
    }

    sent = urls.catalog_api_params(params)

    assert sent["attribute_ids[brand]"] == "209084,563992"
    assert sent["attribute_ids[size]"] == "208,209"
    assert sent["attribute_ids[status]"] == "2,1,6"
    assert sent["attribute_ids[catalog]"] == "2050"
    assert sent["attribute_ids[color]"] == "22"
    assert sent["attribute_ids[material]"] == "44"
    for dead in ("brand_ids", "size_ids", "status_ids", "catalog_ids"):
        assert dead not in sent, f"{dead} would be silently ignored by svc-catalogue"
    # Scalars still work server-side (price_to verified live) and pass through as-is.
    assert sent["search_text"] == "mikina"
    assert sent["order"] == "newest_first"
    assert sent["price_to"] == "80"
