"""The mapped-query contract: what converts to params, and what must never leak into them."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from vinted_sniper.magic.models import MappedQuery, Named, WatchHints


def _full() -> MappedQuery:
    return MappedQuery(
        catalog=Named(id=2052, name="Men's jackets"),
        brand=Named(id=90804, name="Patagonia"),
        sizes=[Named(id=208, name="M"), Named(id=209, name="L")],
        price_to=Decimal("60"),
        currency="EUR",
        search_text="Patagonia Torrentshell",
        keywords=["torrentshell"],
        visual_signature="a shell jacket with a two-way front zip",
        watch_hints=WatchHints(required_keywords=["torrentshell"], title_pattern="(?i)torrent"),
    )


def test_full_query_converts_to_the_expected_params() -> None:
    assert _full().to_params() == {
        "catalog_ids": "2052",
        "brand_ids": "90804",
        "size_ids": "208,209",
        "price_to": "60",
        "currency": "EUR",
        "search_text": "Patagonia Torrentshell",
        "order": "newest_first",
    }


def test_params_are_all_strings() -> None:
    # VintedClient.search(tld, params) takes dict[str, str] and appends to it.
    assert all(isinstance(v, str) for v in _full().to_params().values())


def test_sparse_query_emits_only_what_was_set() -> None:
    params = MappedQuery(search_text="patagonia bunda").to_params()
    assert params == {"search_text": "patagonia bunda", "order": "newest_first"}


def test_empty_query_is_still_an_ordered_search() -> None:
    assert MappedQuery().to_params() == {"order": "newest_first"}


def test_sizes_keep_the_order_they_were_given() -> None:
    q = MappedQuery(sizes=[Named(id=209, name="L"), Named(id=208, name="M")])
    assert q.to_params()["size_ids"] == "209,208"


def test_watch_hints_never_reach_the_search_params() -> None:
    params = _full().to_params()
    assert "required_keywords" not in params
    assert "title_pattern" not in params
    assert "watch_hints" not in params
    # Nor smuggled in under a search key.
    assert "torrent" not in params.get("search_text", "").lower().replace("torrentshell", "")
    assert set(params) <= {
        "catalog_ids",
        "brand_ids",
        "size_ids",
        "price_to",
        "currency",
        "search_text",
        "order",
    }


def test_ranking_fields_are_not_search_params() -> None:
    # keywords and visual_signature drive the sweep's ranking, not the request.
    params = _full().to_params()
    assert "keywords" not in params
    assert "visual_signature" not in params


def test_a_zero_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Named(id=0, name="nothing")


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Named(id=1, name="")


def test_a_negative_price_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedQuery(price_to=Decimal("-1"))


def test_eleven_sizes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedQuery(sizes=[Named(id=i, name=f"s{i}") for i in range(1, 12)])


def test_eleven_keywords_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedQuery(keywords=[f"k{i}" for i in range(11)])


def test_an_overlong_search_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedQuery(search_text="x" * 201)


def test_an_overlong_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MappedQuery(currency="EURO")


def test_unknown_keys_from_the_flow_are_ignored() -> None:
    # An n8n flow that grows a field must not break a running app.
    q = MappedQuery.model_validate(
        {
            "search_text": "patagonia",
            "confidence": 0.91,
            "reasoning": "the user said Patagonia",
        }
    )
    assert q.search_text == "patagonia"
    assert q.to_params() == {"search_text": "patagonia", "order": "newest_first"}


def test_a_bad_nested_id_still_raises_through_the_dict_form() -> None:
    with pytest.raises(ValidationError):
        MappedQuery.model_validate({"brand": {"id": -5, "name": "Patagonia"}})


def test_price_to_parsed_from_json_keeps_its_own_formatting() -> None:
    q = MappedQuery.model_validate({"price_to": "60.50"})
    assert q.to_params()["price_to"] == "60.50"
