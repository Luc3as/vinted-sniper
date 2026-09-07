from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from vinted_sniper.vinted.models import ParseError, parse_item


def catalog_entry(**overrides: Any) -> dict[str, Any]:
    """A catalog entry shaped like the real thing, trimmed to what we read."""
    entry: dict[str, Any] = {
        "id": 9683334896,
        "title": "Nike Air",
        "url": "https://www.vinted.fr/items/9683334896-nike-air",
        "brand_title": "Nike Air",
        "size_title": "38.5",
        "status": "Bon état",
        "price": {"amount": "15.0", "currency_code": "EUR"},
        "total_item_price": {"amount": "16.45", "currency_code": "EUR"},
        "service_fee": {"amount": "1.45", "currency_code": "EUR"},
        "photo": {
            "full_size_url": "https://images.vinted.net/full.jpeg",
            "high_resolution": {"id": "abc", "timestamp": 1_755_374_000},
        },
        "photos": [
            {"full_size_url": "https://images.vinted.net/full.jpeg"},
            {"full_size_url": "https://images.vinted.net/back.jpeg"},
            {"url": "https://images.vinted.net/label.jpeg"},
        ],
        "user": {"id": 7, "login": "seller", "feedback_reputation": 0.93, "feedback_count": 41},
        "promoted": False,
        "favourite_count": 2,
        "view_count": 11,
        "item_box": {"accessibility_label": "Nike Air, brand: Nike, 15,00 €"},
    }
    entry.update(overrides)
    return entry


def test_a_full_entry_parses() -> None:
    item = parse_item(catalog_entry(), "fr")

    assert item.item_id == 9683334896
    assert item.title == "Nike Air"
    assert item.brand == "Nike Air"
    assert item.size == "38.5"
    assert item.condition == "Bon état"
    assert item.price == Decimal("15.0")
    assert item.total_price == Decimal("16.45")
    assert item.currency == "EUR"
    assert item.photo_ts == 1_755_374_000
    assert item.seller_login == "seller"
    assert item.seller_id == 7
    assert item.seller_feedback_count == 41
    assert item.seller_url == "https://www.vinted.fr/member/7"
    assert item.tld == "fr"


def test_an_anonymous_listing_has_no_seller_link() -> None:
    item = parse_item(catalog_entry(user=None), "fr")

    assert item.seller_url is None
    assert item.seller_feedback_count is None


def test_total_price_is_kept_apart_from_the_asking_price() -> None:
    item = parse_item(catalog_entry(), "fr")

    assert item.total_price is not None
    assert item.price is not None
    assert item.total_price > item.price
    assert "with protection" in item.price_line()


def test_price_line_stays_simple_when_there_is_no_fee() -> None:
    item = parse_item(
        catalog_entry(total_item_price={"amount": "15.0", "currency_code": "EUR"}), "fr"
    )

    assert item.price_line() == "15.0 EUR"


def test_a_listing_without_an_id_is_rejected() -> None:
    entry = catalog_entry()
    del entry["id"]

    with pytest.raises(ParseError):
        parse_item(entry, "fr")


@pytest.mark.parametrize(
    "overrides",
    [
        {"photo": None},
        {"photo": []},
        {"user": None},
        {"brand_title": ""},
        {"size_title": None},
        {"price": None},
        {"total_item_price": "not-a-price"},
        {"favourite_count": None},
    ],
)
def test_missing_or_reshaped_fields_do_not_lose_the_listing(overrides: dict[str, Any]) -> None:
    item = parse_item(catalog_entry(**overrides), "fr")

    assert item.item_id == 9683334896
    assert item.title == "Nike Air"


def test_every_photo_is_kept_for_the_gallery() -> None:
    item = parse_item(catalog_entry(), "fr")

    assert item.photo_urls == (
        "https://images.vinted.net/full.jpeg",
        "https://images.vinted.net/back.jpeg",
        "https://images.vinted.net/label.jpeg",
    )


def test_a_listing_without_the_photos_array_still_has_its_cover() -> None:
    entry = catalog_entry()
    del entry["photos"]

    item = parse_item(entry, "fr")

    assert item.photo_urls == ("https://images.vinted.net/full.jpeg",)


def test_junk_in_the_photos_array_does_not_lose_the_gallery() -> None:
    item = parse_item(
        catalog_entry(photos=[None, "text", {"url": "https://images.vinted.net/ok.jpeg"}, {}]),
        "fr",
    )

    assert item.photo_urls == ("https://images.vinted.net/ok.jpeg",)


def test_no_label_brand_reads_as_absent() -> None:
    assert parse_item(catalog_entry(brand_title="NO LABEL"), "fr").brand is None


def test_bare_numeric_price_is_accepted() -> None:
    item = parse_item(catalog_entry(price=12.5), "fr")

    assert item.price == Decimal("12.5")


def test_url_is_derived_when_the_payload_omits_it() -> None:
    entry = catalog_entry()
    del entry["url"]

    assert parse_item(entry, "de").url == "https://www.vinted.de/items/9683334896"


def test_raw_payload_is_only_kept_when_asked_for() -> None:
    assert parse_item(catalog_entry(), "fr").raw is None
    assert parse_item(catalog_entry(), "fr", keep_raw=True).raw is not None


def test_action_links_follow_the_country_site() -> None:
    item = parse_item(catalog_entry(), "co.uk")

    assert item.message_url.startswith("https://www.vinted.co.uk/")
    assert item.buy_url.startswith("https://www.vinted.co.uk/")


def thumbnails() -> list[dict[str, Any]]:
    """The variant array as the catalog response carries it, smallest first."""
    return [
        {
            "type": "thumb70x100",
            "width": 70,
            "height": 100,
            "url": "https://images.vinted.net/70.jpeg",
        },
        {
            "type": "thumb310x430",
            "width": 310,
            "height": 430,
            "url": "https://images.vinted.net/310.jpeg",
        },
        {
            "type": "thumb800x1120",
            "width": 800,
            "height": 1120,
            "url": "https://images.vinted.net/800.jpeg",
        },
    ]


def entry_with_thumbnails(variants: Any) -> dict[str, Any]:
    entry = catalog_entry()
    entry["photo"] = {**entry["photo"], "thumbnails": variants}
    return entry


def test_thumb_url_picks_the_variant_closest_to_the_target_width() -> None:
    item = parse_item(entry_with_thumbnails(thumbnails()), "fr")

    assert item.thumb_url == "https://images.vinted.net/310.jpeg"
    assert item.photo_url == "https://images.vinted.net/full.jpeg"


def test_thumb_url_takes_the_nearest_width_when_the_exact_size_is_absent() -> None:
    variants = [v for v in thumbnails() if v["width"] != 310]
    variants.append({"type": "thumbXL", "width": 364, "url": "https://images.vinted.net/364.jpeg"})

    assert parse_item(entry_with_thumbnails(variants), "fr").thumb_url == (
        "https://images.vinted.net/364.jpeg"
    )


def test_thumb_url_falls_back_to_the_full_photo_when_there_are_no_thumbnails() -> None:
    item = parse_item(catalog_entry(), "fr")

    assert item.thumb_url == item.photo_url == "https://images.vinted.net/full.jpeg"


def test_malformed_thumbnail_entries_are_skipped_rather_than_raising() -> None:
    variants: list[Any] = [
        "https://images.vinted.net/bare-string.jpeg",
        {"type": "thumb310x430", "url": "https://images.vinted.net/no-width.jpeg"},
        {"width": 320, "url": None},
        {"width": "310", "url": "https://images.vinted.net/string-width.jpeg"},
    ]

    assert parse_item(entry_with_thumbnails(variants), "fr").thumb_url == (
        "https://images.vinted.net/string-width.jpeg"
    )


def test_an_entirely_unusable_thumbnail_array_falls_back_to_the_full_photo() -> None:
    for variants in ([], ["nope", {"width": 310}, {"url": "https://x/y.jpeg"}], "not-a-list"):
        item = parse_item(entry_with_thumbnails(variants), "fr")

        assert item.thumb_url == "https://images.vinted.net/full.jpeg"
