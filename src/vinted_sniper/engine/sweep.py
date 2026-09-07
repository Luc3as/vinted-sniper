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
pages the API in relevance order, funnels what it read and writes the result to the sweep
tables. `judge_sweep()` is the whole point of the milestone: it runs that sweep, sends
every survivor's *thumbnail* to the photo check so what comes back is ordered by what the
listings look like rather than by what the sellers called them, and then buys a full
opinion on the best two or three — a hard cap applied to a list before any request is
made, and a `sweep.cost` line saying what the whole run spent.

`engine/dedup.py` is deliberately not used — its freshness window is newest-first semantics
and would discard nearly all of an existing-stock sweep.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from vinted_sniper.db.repo import Query, Repo, SweepCandidate
from vinted_sniper.engine import filters
from vinted_sniper.log import get_logger
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageItem, TriageTarget
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

if TYPE_CHECKING:  # pragma: no cover - the stages are duck-typed at runtime, see judge_sweep()
    from vinted_sniper.magic.triage import TriageClient
    from vinted_sniper.magic.verdict import VerdictClient

log = get_logger(__name__)

# How long a sweep holds the whole site after a refusal. The poller scales its backoff by
# the search's poll interval; a sweep has no interval to scale, so it takes a flat middle
# figure — long enough to be a real hold, short enough that one refused sweep does not
# silence the standing pollers for the rest of the hour.
BLOCKED_COOLDOWN_S = 300.0


@dataclass(frozen=True, slots=True)
class RankedItem:
    """A listing the funnel kept, with the scores that decide where it sits.

    `rank_score` is what the seller's title earned. The three triage fields are what the
    photo check said about it and stay at their defaults until `judge_sweep()` fills them
    in, so `funnel()` and `run_sweep()` keep producing exactly the object they always did.

    `matches_target` is three-valued for the same reason the column is (MEM/T02): `None`
    means nobody looked, `False` means the model looked and said no. Collapsing the two
    would let a batch that never came back rank as a rejection.
    """

    item: Item
    rank_score: float
    matches_target: bool | None = None
    confidence: float | None = None
    triage_reason: str | None = None


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
    # How many candidates came back from the photo check with an answer. Never more than
    # len(candidates), and lower than it whenever a batch failed or a flow skipped an id.
    triaged: int = 0
    # Full photo verdicts paid for. Filled by T06; carried here so one object still
    # describes the whole run rather than the caller stitching two together.
    verdicts: int = 0
    # What the AI stages spent, as the flows reported it. Additive across batches.
    tokens: int = 0
    cost_eur: float = 0.0


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


async def _store_run(
    repo: Repo,
    sweep_id: int,
    candidates: list[SweepCandidate],
    result: SweepResult,
    *,
    pages_fetched: int,
    status: str,
    error: str | None,
    close: bool,
) -> None:
    """Write what the paging found, and close the row unless the caller owns more of the run."""
    await repo.record_sweep_candidates(sweep_id, candidates)
    if not close:
        return
    await repo.finish_sweep_run(
        sweep_id,
        status=status,
        pages_fetched=pages_fetched,
        items_seen=result.items_seen,
        candidates=len(candidates),
        funnel=result.funnel,
        error=error,
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
    sweep_id: int | None = None,
    close: bool = True,
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

    `close=False` is for a caller that owns more of the run than this function does — the
    photo check and the verdicts in `judge_sweep()` run for minutes after the last page is
    read. Closing the row here would set `status='ok'` and `finished_at` while those stages
    are still spending money, and anything reading the row (the browser polling
    `GET /api/sweeps/{id}` above all) would call the sweep finished before it was. The
    candidates are still written either way; only the row's closing is deferred.

    `sweep_id` is for callers that need the id *before* the sweep starts — an HTTP handler
    that has to answer 202 with something the browser can poll, when the run itself will
    still be reading pages minutes later. Passing one means "the row is already open, use
    it"; leaving it None runs the line below exactly as every existing caller does. Same
    shape as `filters.check(gates=...)`: an optional keyword that leaves the default path
    byte-identical rather than a branch through the body.
    """
    started = time.monotonic()
    if sweep_id is None:
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
    await _store_run(
        repo,
        sweep_id,
        stored,
        result,
        pages_fetched=pages_fetched,
        status=status,
        error=error,
        close=close,
    )
    # One line per run, carrying the whole funnel. A sweep that returned little is
    # explained by grepping this event rather than by re-running it: the per-reason drop
    # counts say whether the budget gate, the banned words or the site itself ate the page.
    run_log.info(
        "sweep.summary",
        sweep_id=sweep_id,
        pages_fetched=pages_fetched,
        items_seen=result.items_seen,
        candidates=len(stored),
        funnel=result.funnel,
        status=status,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    return replace(
        result,
        pages_fetched=pages_fetched,
        sweep_id=sweep_id,
        status=status,
        error=error,
    )


async def _close_run(repo: Repo, result: SweepResult, status: str, error: str | None) -> None:
    """Close a judged run's row with the counts `run_sweep()` established.

    Only `judge_sweep()` calls this, and it calls it exactly once, on every path out. The
    row therefore reads `running` for as long as the run is running — including the minutes
    the photo check and the verdicts take, which is the whole reason `run_sweep(close=False)`
    exists. A poller that stops at "no longer running" can be believed.
    """
    await repo.finish_sweep_run(
        result.sweep_id,
        status=status,
        pages_fetched=result.pages_fetched,
        items_seen=result.items_seen,
        candidates=len(result.candidates),
        funnel=result.funnel,
        error=error,
    )


async def judge_sweep(
    *,
    tld: str,
    params: dict[str, str],
    keywords: list[str],
    visual_signature: str | None,
    labels: dict[str, str],
    client: VintedClient,
    repo: Repo,
    triage: TriageClient,
    max_pages: int,
    max_items: int,
    batch_size: int,
    verdict: VerdictClient | None = None,
    max_verdicts: int = 0,
    query_id: int | None = None,
    gates_query: Query | None = None,
    sessions: SessionManager | None = None,
    sweep_id: int | None = None,
    cost_per_mtok_in: float = 0.0,
    cost_per_mtok_out: float = 0.0,
) -> SweepResult:
    """Run a sweep, re-rank it by what the photos show, then buy a few full opinions.

    This is the point of the milestone in one function. `run_sweep()` can only rank what a
    seller typed, and in the reference sweep not one of the 89 real matches named the model
    in its title (R003/R005). So every survivor goes to the photo check, and the order that
    comes back out puts a listing triage *recognised* above a listing whose title matched
    and which triage rejected. Confidence ranks; it never gates — dropping a low-confidence
    listing before the verdict stage would recreate exactly the failure R003 is about.

    Two things it deliberately does not do:

    * **It does not touch `run_sweep()`.** It calls it and adds stages after it, so the
      isolation guarantees pinned in `tests/integration/test_sweep_isolation.py` keep
      testing the same code they were written against.
    * **It does not run the batches concurrently.** They are independent and could, but
      concurrency against a single n8n instance is a new failure mode (partial batches,
      rate limits, an unclear execution history) and this slice is not the place to take it
      on. If a sweep of a few hundred items ever needs to be faster, that is an S04
      concern and belongs behind a bounded gather, not a bare `asyncio.gather`.

    The verdict stage is the expensive one and the cap on it is structural: the post-triage
    order is filtered to what the photo check recognised and sliced to `max_verdicts`
    *before* the loop starts, so "at most three" is a property of a list rather than a
    sentence in a prompt. Zero matches and `max_verdicts=0` both mean zero requests and
    neither is an error, and a `verdict` client of `None` turns the stage off entirely.

    A sweep never raises. A refused or partial `run_sweep()` comes straight back
    untouched — a run that could not read the site must not then go and spend money on it —
    and a triage batch that fails closes the run `status='partial'`, returns what was
    triaged so far and skips the verdict stage for the same reason. A verdict that fails is
    narrower still: that one candidate is logged and skipped, the rest are bought, and the
    run closes `partial`. Nothing already stored is discarded, and neither is the bill for
    it — `add_sweep_cost()` is called as each stage completes, so a run that dies half way
    through still reports what it actually spent.

    `sweep_id` is forwarded to `run_sweep()` untouched and means the same thing here: the
    run's row is already open, so a caller that answered 202 with an id minutes ago is
    still describing this run and not a second one.

    Ordering note: the stored `position` column stays the funnel's order. The triage order
    is the returned `candidates` list, and it is reproducible from the database at any time
    because `matches_target`, `confidence` and `rank_score` are all persisted per candidate.
    """
    result = await run_sweep(
        tld=tld,
        params=params,
        keywords=keywords,
        client=client,
        repo=repo,
        max_pages=max_pages,
        max_items=max_items,
        query_id=query_id,
        gates_query=gates_query,
        sessions=sessions,
        sweep_id=sweep_id,
        # This function closes the row, once, at whichever point the run actually ends.
        close=False,
    )
    if result.status != "ok" or not result.candidates:
        # Nothing to judge: this is that point.
        await _close_run(repo, result, result.status, result.error)
        return result

    run_log = log.bind(sweep_id=result.sweep_id, tld=tld)
    target = TriageTarget(
        keywords=list(keywords), visual_signature=visual_signature, labels=dict(labels)
    )

    outcomes: dict[int, TriageItem] = {}
    tokens = 0
    cost_eur = 0.0
    status, error = result.status, result.error

    for batch in _batches(result.candidates, batch_size):
        sent = {ranked.item.item_id for ranked in batch}
        try:
            answer = await triage.judge([ranked.item for ranked in batch], target)
        except MappingError as exc:
            # S01's discipline: the sweep degrades, it does not raise. Earlier batches are
            # already on disk and stay there.
            status, error = "partial", str(exc)
            run_log.warning("sweep.triage_failed", error=error, triaged=len(outcomes))
            break

        keep = [item for item in answer.results if item.id in sent]
        returned = {item.id for item in answer.results}
        unknown = sorted(returned - sent)
        missing = sorted(sent - returned)
        if unknown or missing:
            # Neither is fatal — an id nobody sent is dropped and an id nobody answered
            # stays un-triaged — but both silently change the ranking, so both are said out
            # loud once per batch rather than being inferred from a short result list.
            run_log.warning(
                "magic.triage_mismatch",
                sent=len(sent),
                returned=len(returned),
                unknown=unknown,
                missing=missing,
            )

        # Written per batch, not at the end: a run that dies on batch 7 of 10 leaves six
        # batches' worth of answers on disk instead of none.
        await repo.record_triage(result.sweep_id, keep)
        for item in keep:
            outcomes[item.id] = item

        batch_tokens, batch_cost = _usage_cost(answer.usage, cost_per_mtok_in, cost_per_mtok_out)
        tokens += batch_tokens
        cost_eur += batch_cost
        if batch_tokens or batch_cost:
            await repo.add_sweep_cost(result.sweep_id, batch_tokens, batch_cost)

    judged = [
        replace(
            ranked,
            matches_target=outcome.matches_target if outcome else None,
            confidence=outcome.confidence if outcome else None,
            triage_reason=outcome.reason if outcome else None,
        )
        for ranked in result.candidates
        for outcome in (outcomes.get(ranked.item.item_id),)
    ]
    judged.sort(key=_triage_rank_key)

    verdicts = 0
    if verdict is not None and status == result.status:
        # The cap is applied here, to a list, before a single request exists — which is the
        # only form of "at most three" a test can hold the code to. A cap asked for in a
        # prompt is a wish. `max_verdicts=0` and "nothing matched" both slice to an empty
        # list and cost nothing, and neither is an error.
        #
        # A triage stage that failed partway does not get to open the expensive one: the
        # same rule that stops a blocked `run_sweep()` reaching the photo check. The
        # answers triage did pay for are already on disk and already reported.
        winners = [ranked for ranked in judged if ranked.matches_target][:max_verdicts]
        verdicts, spent_tokens, spent_cost, failure = await _buy_verdicts(
            winners,
            verdict=verdict,
            target=target,
            repo=repo,
            sweep_id=result.sweep_id,
            run_log=run_log,
            cost_per_mtok_in=cost_per_mtok_in,
            cost_per_mtok_out=cost_per_mtok_out,
        )
        tokens += spent_tokens
        cost_eur += spent_cost
        if failure is not None:
            status, error = "partial", failure

    await _close_run(repo, result, status, error)

    matched = sum(1 for ranked in judged if ranked.matches_target)
    run_log.info(
        "sweep.judged",
        candidates=len(judged),
        triaged=len(outcomes),
        matched=matched,
        verdicts=verdicts,
        batches=_batch_count(len(result.candidates), batch_size),
        status=status,
    )
    # The bill, on its own line, once per judged run. Five fields, because those five are
    # what R004 asks a run to be able to answer: how much was looked at for free, how much
    # was looked at cheaply, how much was looked at properly, and what the two paid stages
    # cost between them. Grepping `sweep.cost` is the whole cost report.
    run_log.info(
        "sweep.cost",
        items_funneled=len(result.candidates),
        thumbnails_triaged=len(outcomes),
        verdicts_issued=verdicts,
        tokens=tokens,
        cost_eur=round(cost_eur, 4),
    )
    return replace(
        result,
        candidates=judged,
        triaged=len(outcomes),
        verdicts=verdicts,
        tokens=tokens,
        cost_eur=cost_eur,
        status=status,
        error=error,
    )


async def _buy_verdicts(
    winners: Sequence[RankedItem],
    *,
    verdict: VerdictClient,
    target: TriageTarget,
    repo: Repo,
    sweep_id: int,
    run_log: structlog.stdlib.BoundLogger,
    cost_per_mtok_in: float,
    cost_per_mtok_out: float,
) -> tuple[int, int, float, str | None]:
    """Buy one full opinion per winner and return what was bought, spent and lost.

    The list arriving here is already capped — that is deliberate, and it is why this
    function has no ceiling of its own: there is exactly one place that decides how many
    verdicts a sweep pays for, and it is the slice in `judge_sweep()` above.

    A refusal is per candidate. It is reported back as a message rather than raised, so the
    caller closes the run `partial` while the verdicts either side of the failure stay
    bought, stored and billed. The last failure wins the message; the count of them is in
    the `sweep.verdict_failed` lines.
    """
    bought = 0
    tokens = 0
    cost_eur = 0.0
    failure: str | None = None

    for ranked in winners:
        item_id = ranked.item.item_id
        try:
            opinion = await verdict.judge(ranked.item, target)
        except MappingError as exc:
            failure = str(exc)
            run_log.warning("sweep.verdict_failed", item_id=item_id, error=failure)
            continue

        # `record_verdict`, never `store_enrichment` — same answer shape, different table,
        # and the wrong one here writes nothing at all (D005/D009/MEM007).
        await repo.record_verdict(sweep_id, item_id, opinion.verdict, int(time.time()))
        bought += 1
        spent_tokens, spent_cost = _usage_cost(opinion.usage, cost_per_mtok_in, cost_per_mtok_out)
        tokens += spent_tokens
        cost_eur += spent_cost
        # Per verdict, like the triage batches: a run that dies on the third of three still
        # shows what the first two cost.
        if spent_tokens or spent_cost:
            await repo.add_sweep_cost(sweep_id, spent_tokens, spent_cost)

    return bought, tokens, cost_eur, failure


def _batches(candidates: Sequence[RankedItem], size: int) -> Iterator[list[RankedItem]]:
    """Chunk the survivors into the groups one request each will carry.

    A size of zero or less would loop forever rather than fail loudly, so it is clamped to
    one batch of everything — the config field is bounded `ge=1`, and a caller passing 0 by
    hand should get one expensive request, not a hang.
    """
    step = max(1, size) if size > 0 else len(candidates) or 1
    for start in range(0, len(candidates), step):
        yield list(candidates[start : start + step])


def _batch_count(total: int, size: int) -> int:
    step = max(1, size) if size > 0 else total or 1
    return -(-total // step)


def _usage_cost(usage: object, per_mtok_in: float, per_mtok_out: float) -> tuple[int, float]:
    """What one call cost: the flow's own figure when it gave one, arithmetic otherwise.

    Shared by both paid stages — a triage batch and a single verdict are priced by the same
    rule, so an operator comparing the two lines is comparing like with like.

    A flow that reports nothing is not an error — it just means the run's cost line is an
    estimate from the configured per-million rates. The rates default to 0.0 so a caller
    that has not wired them in reports tokens and no money, rather than a made-up price.
    """
    if usage is None:
        return 0, 0.0
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    reported = getattr(usage, "cost_eur", None)
    if reported is not None:
        return tokens_in + tokens_out, float(reported)
    estimated = (tokens_in * per_mtok_in + tokens_out * per_mtok_out) / 1_000_000
    return tokens_in + tokens_out, estimated


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
        # The small variant T01 parsed. Without it the column 0015 added is NULL on every
        # stored row, and every reader — the API, the history page — has to fall back to a
        # full-size photo it did not need.
        thumb_url=item.thumb_url,
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


def _triage_rank_key(ranked: RankedItem) -> tuple[int, float, float, Decimal, int]:
    """Recognised first, then un-triaged, then rejected — and never a coin flip.

    The whole slice is this tuple. A listing whose title said nothing but whose photo the
    model recognised outranks a listing whose title matched and whose photo it rejected,
    because `matches_target` is the first term and `rank_score` only the third.

    The same total-ordering discipline as `_rank_key`: two items with equal confidence and
    equal title score still order by price and then by id, so "the top 3" means the same
    three listings on a re-run. Un-triaged sorts as 0 — below a confirmed match, above a
    rejection — so a batch that never came back costs a listing its place in the queue
    without costing it the run.

    Within the rejected band `-confidence` puts the confidently-rejected first, which is
    inert: nothing downstream reads past the matches, and it keeps the tuple one rule
    rather than one rule with an exception in it.
    """
    item = ranked.item
    payable = item.total_price if item.total_price is not None else item.price
    return _triage_key(
        ranked.matches_target, ranked.confidence, ranked.rank_score, payable, item.item_id
    )


def triage_order(candidates: Sequence[SweepCandidate]) -> list[SweepCandidate]:
    """Stored rows put back into the order `judge_sweep()` handed them over in.

    T05 deliberately kept `sweep_candidates.position` as the funnel's order rather than
    rewriting it to the triage order, so that the ranking has one source of truth. This is
    the other half of that decision: the triage order is *derived* on read, from the
    `matches_target`, `confidence` and `rank_score` that were persisted per candidate, by
    the same rule `judge_sweep()` sorted with. A reader — the API, the history page —
    therefore never has to reimplement the ordering, and cannot drift from it.
    """
    return sorted(candidates, key=_stored_triage_key)


def _stored_triage_key(candidate: SweepCandidate) -> tuple[int, float, float, Decimal, int]:
    """`_triage_rank_key` for a database row: same rule, different accessors."""
    payable = candidate.total_price if candidate.total_price is not None else candidate.price
    return _triage_key(
        candidate.matches_target,
        candidate.confidence,
        candidate.rank_score,
        Decimal(str(payable)) if payable is not None else None,
        candidate.item_id,
    )


def _triage_key(
    matches_target: bool | None,
    confidence: float | None,
    rank_score: float,
    payable: Decimal | None,
    item_id: int,
) -> tuple[int, float, float, Decimal, int]:
    """The one place the post-triage order is written down. Read it in `_triage_rank_key`."""
    return (
        -_match_rank(matches_target),
        -(confidence or 0.0),
        -rank_score,
        payable if payable is not None else Decimal("Infinity"),
        item_id,
    )


def _match_rank(matches_target: bool | None) -> int:
    """True -> 1, never looked -> 0, rejected -> -1. The three-valued column, ordered."""
    if matches_target is None:
        return 0
    return 1 if matches_target else -1


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Kurtká" and "kurtka" are the same word."""
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))
