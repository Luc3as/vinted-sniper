"""The sweep funnel: what it keeps, what it drops, and in what order.

The load-bearing test here is the first one. A sweep exists to rank existing stock, so a
listing whose title says nothing the search asked for must still be a candidate — that is
the whole difference between this and the standing poller.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from vinted_sniper.db.repo import Query
from vinted_sniper.engine import filters, sweep
from vinted_sniper.vinted.models import Item, parse_item

PHOTO_TS = 1_760_000_000


@pytest.fixture
def listing(make_item: Callable[..., dict[str, Any]]) -> Callable[..., Item]:
    """A parsed listing, built from the same catalog shape the API returns."""

    def _build(item_id: int, **overrides: Any) -> Item:
        return parse_item(make_item(item_id, photo_ts=PHOTO_TS, **overrides), "fr")

    return _build


def query(**overrides: Any) -> Query:
    return sweep.ephemeral_query(tld="fr", **overrides)


# --- Title ranking, never gating ----------------------------------------------------


def test_r003_a_title_with_no_keyword_matches_is_still_a_candidate(
    listing: Callable[..., Item],
) -> None:
    """R003: sellers omit the model name, so the title ranks and never eliminates.

    "Kurtka Patagonia" matches none of the keywords a Torrentshell search would carry, but
    it is exactly the listing this feature exists to surface. It must appear in candidates,
    below the title that does match.
    """
    vague = listing(1, title="Kurtka Patagonia")
    explicit = listing(2, title="Patagonia Torrentshell 3L jacket")

    result = sweep.funnel(
        [vague, explicit],
        query(),
        ["torrentshell", "jacket"],
        max_items=10,
    )

    kept = [candidate.item.item_id for candidate in result.candidates]
    assert kept == [2, 1], "the vague title ranks last but is kept"
    assert result.funnel == {}, "nothing was dropped"
    assert result.candidates[0].rank_score == 1.0
    assert result.candidates[1].rank_score == 0.0, "zero matches is a score, not a rejection"


def test_score_title_counts_distinct_keywords_ignoring_case_and_accents() -> None:
    assert sweep.score_title("Patagonia Torrentshell", ["patagonia", "torrentshell"]) == 1.0
    assert sweep.score_title("PATAGONIA jacket", ["patagonia", "torrentshell"]) == 0.5
    assert sweep.score_title("Kurtká Patagonia", ["kurtka"]) == 1.0
    assert sweep.score_title("jacket jacket jacket", ["jacket"]) == 1.0, "distinct, not counted"
    assert sweep.score_title("anything", []) == 0.0, "no keywords means nothing to score"
    assert sweep.score_title("anything", ["  "]) == 0.0, "blank keywords do not divide by zero"


def test_a_promoted_listing_survives_the_sweep_gates(listing: Callable[..., Item]) -> None:
    """Bumped listings are ordinary in standing stock; the default gates would drop them."""
    bumped = listing(1, promoted=True)

    assert filters.check(bumped, query()) == filters.Rejection("promoted", "listing is a paid bump")

    result = sweep.funnel([bumped], query(), ["item"], max_items=10)

    assert [c.item.item_id for c in result.candidates] == [1]
    assert "promoted" not in result.funnel


def test_required_keywords_and_title_pattern_drop_nothing_under_sweep_gates(
    listing: Callable[..., Item],
) -> None:
    """Even if a caller smuggles the title gates onto the Query, SWEEP_GATES ignores them."""
    hostile = Query(
        id=7,
        name="stored",
        url="https://www.vinted.fr/catalog?search_text=torrentshell",
        tld="fr",
        params={},
        poll_interval_s=60,
        paused=False,
        required_keywords=["torrentshell"],
        title_pattern=r"^Torrentshell",
    )
    vague = listing(1, title="Kurtka Patagonia")

    assert filters.check(vague, hostile) is not None, "the poller's gates would drop this"

    result = sweep.funnel([vague], hostile, ["torrentshell"], max_items=10)

    assert [c.item.item_id for c in result.candidates] == [1]
    assert result.funnel == {}


# --- Real constraints still drop, and are counted -----------------------------------


def test_over_budget_listings_drop_and_are_counted(listing: Callable[..., Item]) -> None:
    # make_item's total is price * 1.1 + 0.7, so 100.0 is payable 110.7.
    result = sweep.funnel(
        [listing(1, price="100.0"), listing(2, price="10.0")],
        query(max_total_price=Decimal("50")),
        ["item"],
        max_items=10,
    )

    assert [c.item.item_id for c in result.candidates] == [2]
    assert result.funnel == {"over_budget": 1}


def test_a_listing_with_no_price_is_dropped_under_its_own_reason(
    make_item: Callable[..., dict[str, Any]],
) -> None:
    entry = make_item(1, photo_ts=PHOTO_TS)
    entry["price"] = None
    entry["total_item_price"] = None
    priceless = parse_item(entry, "fr")

    result = sweep.funnel([priceless], query(max_total_price=Decimal("50")), [], max_items=10)

    assert result.candidates == []
    assert result.funnel == {"no_price": 1}


def test_banned_keywords_still_drop(listing: Callable[..., Item]) -> None:
    result = sweep.funnel(
        [listing(1, title="Patagonia jacket REPLICA"), listing(2, title="Patagonia jacket")],
        query(banned_keywords=["replica"]),
        ["patagonia"],
        max_items=10,
    )

    assert [c.item.item_id for c in result.candidates] == [2]
    assert result.funnel == {"banned_keyword": 1}


def test_wrong_condition_blocked_seller_rating_and_reviews_all_drop_and_are_counted(
    listing: Callable[..., Item],
) -> None:
    items = [
        listing(1, status="Satisfactory"),
        listing(2, user={"id": 2, "login": "scammer", "feedback_reputation": 0.99}),
        listing(3, user={"id": 3, "login": "newbie", "feedback_reputation": 0.4}),
        listing(4, user={"id": 4, "login": "unrated"}),
        listing(5, user={"id": 5, "login": "good", "feedback_reputation": 0.99}),
    ]

    result = sweep.funnel(
        items,
        query(
            conditions=["Very good"],
            blocked_sellers=["Scammer"],
            min_seller_rating=0.8,
            min_seller_reviews=5,
        ),
        ["item"],
        max_items=10,
    )

    # 5 is the only listing with the right condition, an allowed seller and a good rating,
    # and it is dropped last: nobody in the fixture has a review count.
    assert result.candidates == []
    assert result.funnel == {
        "condition": 1,
        "blocked_seller": 1,
        "seller_rating": 2,
        "seller_reviews": 1,
    }


def test_seller_review_floor_keeps_a_seller_with_enough_history(
    listing: Callable[..., Item],
) -> None:
    reviewed = listing(
        1, user={"id": 1, "login": "veteran", "feedback_reputation": 0.95, "feedback_count": 300}
    )
    thin = listing(
        2, user={"id": 2, "login": "rookie", "feedback_reputation": 0.95, "feedback_count": 2}
    )

    result = sweep.funnel([reviewed, thin], query(min_seller_reviews=5), [], max_items=10)

    assert [c.item.item_id for c in result.candidates] == [1]
    assert result.funnel == {"seller_reviews": 1}


# --- Ordering and the ceiling -------------------------------------------------------


def test_max_items_keeps_the_highest_scoring_not_the_first_seen(
    listing: Callable[..., Item],
) -> None:
    """The ceiling is a spend limit; spending it on page one would defeat the sweep."""
    items = [
        listing(1, title="Kurtka damska"),
        listing(2, title="Patagonia bunda"),
        listing(3, title="Patagonia Torrentshell jacket"),
        listing(4, title="Nike hoodie"),
    ]

    result = sweep.funnel(items, query(), ["patagonia", "torrentshell"], max_items=2)

    assert [c.item.item_id for c in result.candidates] == [3, 2]
    assert result.items_seen == 4, "items_seen counts what was read, not what survived"


def test_ties_break_on_cheapest_then_item_id(listing: Callable[..., Item]) -> None:
    items = [
        listing(30, title="Patagonia jacket", price="30.0"),
        listing(10, title="Patagonia jacket", price="10.0"),
        listing(11, title="Patagonia jacket", price="10.0"),
    ]

    result = sweep.funnel(items, query(), ["patagonia"], max_items=10)

    assert [c.item.item_id for c in result.candidates] == [10, 11, 30]


def test_reordering_the_input_does_not_change_the_output(listing: Callable[..., Item]) -> None:
    titles = ["Patagonia Torrentshell", "Patagonia bunda", "Kurtka", "Torrentshell 3L"]
    forwards = [listing(i, title=title) for i, title in enumerate(titles, start=1)]
    backwards = list(reversed(forwards))

    keywords = ["patagonia", "torrentshell"]
    one = sweep.funnel(forwards, query(), keywords, max_items=10)
    other = sweep.funnel(backwards, query(), keywords, max_items=10)

    assert [c.item.item_id for c in one.candidates] == [c.item.item_id for c in other.candidates]


def test_an_empty_page_is_an_empty_result_not_an_error() -> None:
    result = sweep.funnel([], query(), ["patagonia"], max_items=10)

    assert result.candidates == []
    assert result.funnel == {}
    assert result.items_seen == 0
    assert result.pages_fetched == 0


def test_a_zero_ceiling_keeps_nothing(listing: Callable[..., Item]) -> None:
    result = sweep.funnel([listing(1)], query(), ["item"], max_items=0)

    assert result.candidates == []
    assert result.items_seen == 1, "it was read and passed the gates; the ceiling cut it"


# --- The ephemeral query ------------------------------------------------------------


def test_ephemeral_query_is_unsaved_and_carries_no_title_gates() -> None:
    built = sweep.ephemeral_query(
        tld="cz",
        params={"search_text": "torrentshell"},
        max_total_price=Decimal("80"),
    )

    assert built.id == 0, "0 marks a query that has no row"
    assert built.required_keywords == []
    assert built.title_pattern is None
    assert built.max_total_price == Decimal("80")
    assert built.params == {"search_text": "torrentshell"}


def test_sweep_gates_omit_the_three_gates_that_would_sabotage_a_sweep() -> None:
    names = {gate.__name__ for gate in filters.SWEEP_GATES}

    assert "_promoted" not in names
    assert "_required" not in names
    assert "_title_pattern" not in names
    assert names == {
        "_banned",
        "_price",
        "_condition",
        "_blocked_seller",
        "_seller_rating",
        "_seller_reviews",
    }
