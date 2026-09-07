"""Ranking existing stock, rather than watching for new arrivals.

The standing poller answers "what appeared since I last looked". A sweep answers a
different question — "of everything already on sale, which are worth a look" — and that
changes what the title is allowed to do. Vinted's `search_text` is a relevance hint, not a
filter: sellers write "Kurtka Patagonia", "Patagonia bunda", "Patagonia jacket M" and mean
the same coat. A search for "Torrentshell" that dropped every title without that word would
throw away most of the stock it was meant to find. So here the title *ranks* a listing and
never eliminates it: zero keyword matches is a score of 0.0, which sorts last but still
gets stored.

What does eliminate a listing is a real constraint — banned words, budget, condition,
seller — which is exactly the subset `filters.SWEEP_GATES` runs.

Everything in this module is pure: it takes parsed listings and returns a ranking. No HTTP,
no database, no asyncio. `engine/dedup.py` is deliberately not used — its freshness window
is newest-first semantics and would discard nearly all of an existing-stock sweep.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from vinted_sniper.db.repo import Query
from vinted_sniper.engine import filters
from vinted_sniper.vinted.models import Item


@dataclass(frozen=True, slots=True)
class RankedItem:
    """A listing the funnel kept, with the score that decided where it sits."""

    item: Item
    rank_score: float


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep found: how much it read, what it threw away and why, what it kept."""

    pages_fetched: int = 0
    items_seen: int = 0
    # Rejection reason to how many listings it dropped. A quiet sweep is explainable by
    # reading this rather than by guessing.
    funnel: dict[str, int] = field(default_factory=dict)
    # Best match first. See funnel() for the ordering.
    candidates: list[RankedItem] = field(default_factory=list)


def score_title(title: str, keywords: list[str]) -> float:
    """How much of what you asked for the title actually says, from 0.0 to 1.0.

    Case- and accent-insensitive, so "bunda" matches "Bunda" and "Kurtká". Counts distinct
    keywords, so repeating a word in the title buys nothing. Zero matches is a legal 0.0:
    it means "ranks last", not "rejected".
    """
    wanted = {folded for word in keywords if (folded := _fold(word))}
    if not wanted:
        return 0.0
    haystack = _fold(title)
    matched = sum(1 for word in wanted if word in haystack)
    return matched / len(wanted)


def funnel(
    items: list[Item],
    query: Query,
    keywords: list[str],
    *,
    max_items: int,
) -> SweepResult:
    """Rank the listings a sweep read, dropping only the ones a real constraint excludes.

    Survivors come back best match first, ties broken by cheapest total price and then by
    item id so the same input always produces the same order. The `max_items` ceiling is
    applied *after* sorting, so it keeps the best candidates rather than the first-seen
    ones — a sweep that read four pages should not be paying for whichever listings
    happened to be on page one.
    """
    drops: dict[str, int] = {}
    survivors: list[RankedItem] = []

    for item in items:
        if rejection := filters.check(item, query, gates=filters.SWEEP_GATES):
            drops[rejection.reason] = drops.get(rejection.reason, 0) + 1
            continue
        survivors.append(RankedItem(item=item, rank_score=score_title(item.title, keywords)))

    survivors.sort(key=_rank_key)

    return SweepResult(
        items_seen=len(items),
        funnel=drops,
        candidates=survivors[:max_items] if max_items >= 0 else survivors,
    )


def ephemeral_query(
    *,
    tld: str,
    params: dict[str, str] | None = None,
    banned_keywords: list[str] | None = None,
    max_total_price: Decimal | None = None,
    conditions: list[str] | None = None,
    blocked_sellers: list[str] | None = None,
    min_seller_rating: float | None = None,
    min_seller_reviews: int | None = None,
    name: str = "sweep",
    url: str = "",
) -> Query:
    """A Query that exists only to feed the gates.

    A sweep runs before there is a saved search to hang it on, but the gates read their
    limits off a Query, so one has to be built in memory. `id=0` marks it as unsaved.
    `required_keywords` and `title_pattern` are pinned empty on purpose: even if a caller
    had them, SWEEP_GATES does not run those gates, and leaving them unset keeps that
    obvious to anyone reading a sweep in a debugger.
    """
    return Query(
        id=0,
        name=name,
        url=url,
        tld=tld,
        params=dict(params or {}),
        poll_interval_s=0,
        paused=False,
        banned_keywords=list(banned_keywords or []),
        max_total_price=max_total_price,
        conditions=list(conditions) if conditions else None,
        required_keywords=[],
        title_pattern=None,
        min_seller_rating=min_seller_rating,
        min_seller_reviews=min_seller_reviews,
        blocked_sellers=list(blocked_sellers or []),
    )


def _rank_key(ranked: RankedItem) -> tuple[float, Decimal, int]:
    """Best score first, then cheapest, then oldest id. Total ordering, no coin flips."""
    item = ranked.item
    payable = item.total_price if item.total_price is not None else item.price
    return (
        -ranked.rank_score,
        payable if payable is not None else Decimal("Infinity"),
        item.item_id,
    )


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Kurtká" and "kurtka" are the same word."""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))
