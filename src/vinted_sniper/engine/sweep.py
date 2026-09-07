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

`funnel()` and everything under it is pure: parsed listings in, a ranking out. `run_sweep()`
is the one impure thing here — it pages the API in relevance order, funnels what it read and
writes the result to the sweep tables. `engine/dedup.py` is deliberately not used — its
freshness window is newest-first semantics and would discard nearly all of an existing-stock
sweep.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal

import structlog

from vinted_sniper.db.repo import Query, Repo, SweepCandidate
from vinted_sniper.engine import filters
from vinted_sniper.log import get_logger
from vinted_sniper.vinted.client import PER_PAGE, VintedClient
from vinted_sniper.vinted.errors import (
    AuthExpiredError,
    BlockedError,
    MalformedResponseError,
    NetworkError,
    RateLimitedError,
    VintedError,
)
from vinted_sniper.vinted.models import Item
from vinted_sniper.vinted.session import SessionManager

log = get_logger(__name__)

# How long a sweep holds the whole site after a refusal. The poller scales its backoff by
# the search's poll interval; a sweep has no interval to scale, so it takes a flat middle
# figure — long enough to be a real hold, short enough that one refused sweep does not
# silence the standing pollers for the rest of the hour.
BLOCKED_COOLDOWN_S = 300.0


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
    # The sweep_runs row this result was written to, or 0 for a funnel() call that never
    # touched the database.
    sweep_id: int = 0
    # "ok", "partial" (an error cut the paging short) or "blocked". A sweep that stopped
    # early still returns whatever it managed to read; the status says not to read the
    # counts as a complete picture.
    status: str = "ok"
    error: str | None = None


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


async def run_sweep(
    *,
    tld: str,
    params: dict[str, str],
    keywords: list[str],
    client: VintedClient,
    repo: Repo,
    max_pages: int,
    max_items: int,
    query_id: int | None = None,
    gates_query: Query | None = None,
    sessions: SessionManager | None = None,
) -> SweepResult:
    """Read up to `max_pages` of existing stock in relevance order and store the best of it.

    The relevance ordering is a per-request override and nothing more: `order=relevance`
    goes into the dict handed to `VintedClient.search()`, which passes unknown keys straight
    through. The caller's `params` — usually a stored search's — is never mutated, and the
    canonical stored URL keeps saying `newest_first`, because that is what the standing
    poller's "only what appeared since last time" semantics are built on.

    Paging stops at the first of three things: the page ceiling, a short page (Vinted asks
    for 96 at a time and `_parse_catalog` drops the pagination block, so a short page is the
    only "that was the end" signal available), or the item ceiling that caps what the later
    AI stages will be asked to look at.

    A sweep that dies partway is a recorded result, not an exception. Whatever pages already
    succeeded are funnelled and stored, the run is closed with `status='partial'` (or
    `'blocked'`), and no Vinted error escapes to the caller — a one-shot read failing is not
    a reason to take down whoever asked for it.
    """
    sweep_id = await repo.create_sweep_run(
        tld=tld, params=params, keywords=keywords, query_id=query_id
    )
    gates = gates_query if gates_query is not None else ephemeral_query(tld=tld, params=params)
    run_log = log.bind(sweep_id=sweep_id, tld=tld)

    seen: set[int] = set()
    collected: list[Item] = []
    pages_fetched = 0
    status = "ok"
    error: str | None = None

    for page in range(1, max_pages + 1):
        # Built fresh every iteration: `params` may be a stored query's own dict, and a
        # sweep must not leave `order=relevance` behind in it.
        request = {**params, "order": "relevance", "page": str(page)}
        try:
            items = await client.search(tld, request)
        except BlockedError as exc:
            status, error = "blocked", str(exc)
            await _hold_site(tld, sessions=sessions, repo=repo, run_log=run_log, error=error)
            break
        except (AuthExpiredError, RateLimitedError, MalformedResponseError, NetworkError) as exc:
            # No retrying here, deliberately. The poller retries because it has to keep
            # watching; a sweep is one-shot, and the honest answer to "the site would not
            # talk to me" is a partial result the caller can re-run.
            status, error = "partial", str(exc)
            run_log.warning("sweep.failed", page=page, error=error, kind=type(exc).__name__)
            break
        except VintedError as exc:  # pragma: no cover - future error types land here
            status, error = "partial", str(exc)
            run_log.warning("sweep.failed", page=page, error=error, kind=type(exc).__name__)
            break

        pages_fetched += 1
        for item in items:
            # Relevance paging is not a stable window: the same listing can appear on two
            # consecutive pages as the ranking shifts under us.
            if item.item_id not in seen:
                seen.add(item.item_id)
                collected.append(item)
        run_log.info("sweep.page", page=page, returned=len(items), total=len(collected))

        if len(items) < PER_PAGE:
            break
        if len(collected) >= max_items:
            break

    result = funnel(collected, gates, keywords, max_items=max_items)
    stored = [_to_candidate(ranked, pos) for pos, ranked in enumerate(result.candidates)]
    await repo.record_sweep_candidates(sweep_id, stored)
    await repo.finish_sweep_run(
        sweep_id,
        status=status,
        pages_fetched=pages_fetched,
        items_seen=result.items_seen,
        candidates=len(stored),
        funnel=result.funnel,
        error=error,
    )
    run_log.info(
        "sweep.finished",
        status=status,
        pages=pages_fetched,
        items_seen=result.items_seen,
        candidates=len(stored),
    )
    return replace(
        result,
        pages_fetched=pages_fetched,
        sweep_id=sweep_id,
        status=status,
        error=error,
    )


async def _hold_site(
    tld: str,
    *,
    sessions: SessionManager | None,
    repo: Repo,
    run_log: structlog.stdlib.BoundLogger,
    error: str,
) -> None:
    """Leave the shared site cooldown exactly as a refused poller would leave it.

    A sweep and the standing pollers share one address and one cooldown gate. If a sweep
    could be refused without closing that gate, every poller on the site would carry on
    asking straight through the refusal — so this mirrors `Poller.tick`'s BlockedError arm:
    drop the session, close the gate, and write the deadline down so a restart still knows
    about it.
    """
    if sessions is None:
        run_log.warning("sweep.blocked", error=error, cooldown_applied=False)
        return
    await sessions.discard_blocked(tld)
    sessions.cooldown.close(tld, BLOCKED_COOLDOWN_S)
    await repo.set_state_value(f"cooldown_until:{tld}", str(int(time.time() + BLOCKED_COOLDOWN_S)))
    run_log.warning(
        "sweep.blocked", error=error, cooldown_applied=True, retry_in_s=round(BLOCKED_COOLDOWN_S)
    )


def _to_candidate(ranked: RankedItem, position: int) -> SweepCandidate:
    """Copy enough of a listing into the sweep tables to show it without re-fetching."""
    item = ranked.item
    return SweepCandidate(
        item_id=item.item_id,
        title=item.title,
        url=item.url,
        rank_score=ranked.rank_score,
        position=position,
        stage="funnel",
        price=float(item.price) if item.price is not None else None,
        total_price=float(item.total_price) if item.total_price is not None else None,
        currency=item.currency,
        brand=item.brand,
        size=item.size,
        condition=item.condition,
        photo_url=item.photo_url,
        photo_urls=list(item.photo_urls),
        seller_login=item.seller_login,
        promoted=item.promoted,
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
