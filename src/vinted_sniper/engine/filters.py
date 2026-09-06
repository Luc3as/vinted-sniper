"""Deciding whether a listing is worth telling you about.

Vinted's own filters get you most of the way there; these cover what its search cannot.
The important one is price: Vinted filters on the asking price, but you pay the asking
price plus buyer protection, so a `price_to=30` search happily returns things that cost you
33. Filtering on the total is the difference between a useful alert and a misleading one.

The rest are about the title and the seller. Vinted's text search is loose — a search for
"Torrentshell" happily returns every Patagonia listing whose description mentions it — so
words that must appear in the title, or a pattern the title must match, cut the noise at
the source. And a bargain from a seller with no history is a different offer from the same
bargain from one with three hundred reviews; the API says which, so the filter can too.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from vinted_sniper.db.repo import Query
from vinted_sniper.vinted.models import Item


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a listing was skipped. Worth keeping around: it is what makes a quiet search
    explainable rather than suspicious."""

    reason: str
    detail: str


def check(item: Item, query: Query) -> Rejection | None:
    """Return why this listing should be skipped, or None if it should be sent."""
    for gate in _GATES:
        if rejection := gate(item, query):
            return rejection
    return None


def _promoted(item: Item, _query: Query) -> Rejection | None:
    if item.promoted:
        # Boosted listings resurface old stock. They are not new, whatever the feed says.
        return Rejection("promoted", "listing is a paid bump")
    return None


def _banned(item: Item, query: Query) -> Rejection | None:
    if banned := _banned_keyword(item, query.banned_keywords):
        return Rejection("banned_keyword", f"title contains {banned!r}")
    return None


def _required(item: Item, query: Query) -> Rejection | None:
    if missing := _missing_keyword(item, query.required_keywords):
        return Rejection("missing_keyword", f"title lacks {missing!r}")
    return None


def _title_pattern(item: Item, query: Query) -> Rejection | None:
    if query.title_pattern and not _pattern(query.title_pattern).search(item.title):
        return Rejection("title_pattern", f"title does not match /{query.title_pattern}/")
    return None


def _price(item: Item, query: Query) -> Rejection | None:
    if query.max_total_price is None:
        return None
    payable = item.total_price if item.total_price is not None else item.price
    if payable is None:
        return Rejection("no_price", "listing has no price to compare")
    if payable > query.max_total_price:
        return Rejection(
            "over_budget",
            f"{payable} above limit {query.max_total_price} (buyer protection included)",
        )
    return None


def _condition(item: Item, query: Query) -> Rejection | None:
    if query.conditions and (item.condition or "").lower() not in {
        condition.lower() for condition in query.conditions
    }:
        return Rejection("condition", f"condition {item.condition!r} not wanted")
    return None


def _blocked_seller(item: Item, query: Query) -> Rejection | None:
    if not query.blocked_sellers or not item.seller_login:
        return None
    if item.seller_login.lower() in {s.strip().lower() for s in query.blocked_sellers}:
        return Rejection("blocked_seller", f"seller {item.seller_login!r} is blocked")
    return None


def _seller_rating(item: Item, query: Query) -> Rejection | None:
    if query.min_seller_rating is None:
        return None
    if item.seller_rating is None:
        return Rejection("seller_rating", "seller has no rating yet")
    if item.seller_rating < query.min_seller_rating:
        return Rejection(
            "seller_rating",
            f"seller rated {item.seller_rating:.0%}, below {query.min_seller_rating:.0%}",
        )
    return None


def _seller_reviews(item: Item, query: Query) -> Rejection | None:
    if query.min_seller_reviews is None:
        return None
    if item.seller_feedback_count is None:
        return Rejection("seller_reviews", "seller review count unknown")
    if item.seller_feedback_count < query.min_seller_reviews:
        return Rejection(
            "seller_reviews",
            f"seller has {item.seller_feedback_count} reviews, below {query.min_seller_reviews}",
        )
    return None


# Cheapest and most common rejections first; the order only affects which reason a
# doubly-unwanted listing is reported under.
_GATES = (
    _promoted,
    _banned,
    _required,
    _title_pattern,
    _price,
    _condition,
    _blocked_seller,
    _seller_rating,
    _seller_reviews,
)


def matches(item: Item, query: Query) -> bool:
    return check(item, query) is None


def _missing_keyword(item: Item, required: list[str]) -> str | None:
    """The first required word that is not in the title. Every listed word must appear."""
    if not required:
        return None
    haystack = item.title.lower()
    for word in required:
        candidate = word.strip().lower()
        if candidate and candidate not in haystack:
            return word
    return None


@functools.lru_cache(maxsize=256)
def _pattern(pattern: str) -> re.Pattern[str]:
    """Compiled once per distinct pattern. An invalid one matches nothing rather than
    crashing the check; validate_pattern() is what tells the person at input time."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(r"(?!)")


def validate_pattern(pattern: str) -> str | None:
    """Why a title pattern will not compile, or None if it is fine."""
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return str(exc)
    return None


def _banned_keyword(item: Item, banned: list[str]) -> str | None:
    if not banned:
        return None
    haystack = item.title.lower()
    for word in banned:
        candidate = word.strip().lower()
        if candidate and candidate in haystack:
            return word
    return None
