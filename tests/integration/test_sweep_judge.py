"""The re-rank that is the reason this milestone exists.

Of 89 real matches in the reference sweep, none named the model in the title (R003/R005).
A title score can therefore only ever be a hint, and the assertion that makes that concrete
is the first test in this file: a listing called nothing but "Kurtka Patagonia" — title
score 0.0 — has to come out *above* a listing whose title matched every keyword and whose
photo the model rejected. Everything else in the slice is plumbing for that sentence.

The photo check is stubbed here and nothing else is: a real `SessionManager`, `VintedClient`
and `Repo` over a scripted transport, so what is asserted is the orchestration — how the
batches were cut, what reached the database and when, and what a failing batch leaves
behind — rather than mocks agreeing with each other. `tests/unit/test_magic_triage.py`
owns the wire contract with the flow itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from tests.conftest import ScriptedTransport
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import sweep
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageBatch, TriageItem, TriageTarget, Usage
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.models import Item
from vinted_sniper.vinted.session import SessionManager

PHOTO_TS = 1_760_000_000

PARAMS = {"search_text": "torrentshell", "order": "newest_first"}
# One keyword, so a title score is either 0.0 or 1.0 and the ranking assertions below read
# as "the title said nothing" versus "the title said everything".
KEYWORDS = ["torrentshell"]
LABELS = {"brand": "Patagonia", "catalog": "Jackets & Coats"}
SIGNATURE = "a boxy waterproof shell with a chest zip pocket and a stowaway hood"


@dataclass(frozen=True)
class Answer:
    """What the stubbed flow says about one listing."""

    matches_target: bool
    confidence: float = 0.5
    reason: str | None = None


class StubTriage:
    """Stands in for the n8n photo check, and records everything it was asked.

    Deliberately not an `httpx` mock: `TriageClient` already has its own tests for the wire
    shape, and what these tests need to see is which ids ended up in which batch.
    """

    def __init__(
        self,
        answers: dict[int, Answer],
        *,
        usage: Usage | None = None,
        fail_on: int | None = None,
        invent: dict[int, list[int]] | None = None,
        skip: set[int] | None = None,
    ) -> None:
        self.answers = answers
        self.usage = usage
        # Which batch (0-based) answers with a MappingError instead of a verdict.
        self.fail_on = fail_on
        # Batch index -> ids to answer with that were never sent.
        self.invent = invent or {}
        # Ids to leave out of the answer entirely.
        self.skip = skip or set()
        self.batches: list[list[int]] = []
        self.targets: list[TriageTarget] = []

    async def judge(self, items: list[Item], target: TriageTarget) -> TriageBatch:
        index = len(self.batches)
        self.batches.append([item.item_id for item in items])
        self.targets.append(target)
        if self.fail_on == index:
            raise MappingError("the photo check answered 502")

        results = [
            TriageItem(
                id=item.item_id,
                matches_target=answer.matches_target,
                confidence=answer.confidence,
                reason=answer.reason,
            )
            for item in items
            if item.item_id not in self.skip
            for answer in (self.answers.get(item.item_id, Answer(matches_target=False)),)
        ]
        results.extend(
            TriageItem(id=item_id, matches_target=True, confidence=1.0)
            for item_id in self.invent.get(index, [])
        )
        return TriageBatch(results=results, usage=self.usage)


def queue_page(transport: ScriptedTransport, entries: list[dict[str, Any]]) -> None:
    """One short page, so the sweep reads it and stops."""
    transport.queue_catalog(entries)


async def judge_over(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    *,
    triage: StubTriage,
    batch_size: int = 20,
    keywords: list[str] | None = None,
    max_pages: int = 4,
    max_items: int = 1000,
) -> sweep.SweepResult:
    sessions = SessionManager(db, transport)
    client = VintedClient(transport, sessions)
    return await sweep.judge_sweep(
        tld="fr",
        params=PARAMS,
        keywords=KEYWORDS if keywords is None else keywords,
        visual_signature=SIGNATURE,
        labels=LABELS,
        client=client,
        repo=repo,
        triage=triage,  # type: ignore[arg-type]
        max_pages=max_pages,
        max_items=max_items,
        batch_size=batch_size,
        sessions=sessions,
    )


def order(result: sweep.SweepResult) -> list[int]:
    return [ranked.item.item_id for ranked in result.candidates]


async def stage_of(db: Any, item_id: int) -> str:
    row = await db.fetch_one("SELECT stage FROM sweep_candidates WHERE item_id = ?", (item_id,))
    return str(row["stage"])


# --- The re-rank ----------------------------------------------------------------------


async def test_a_photo_match_outranks_a_title_match_the_photo_check_rejected(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """R003/R005 as one assertion: what it looks like beats what the seller called it.

    Item 101's title never says "Torrentshell" — it scores 0.0 and the funnel puts it last.
    Item 102's title says it and scores 1.0. The photo check recognises 101 and rejects 102,
    and the order that comes back out has to be the photo check's, not the title's.
    """
    queue_page(
        transport,
        [
            make_item(101, photo_ts=PHOTO_TS, title="Kurtka Patagonia", price="40.0"),
            make_item(102, photo_ts=PHOTO_TS, title="Patagonia Torrentshell", price="40.0"),
        ],
    )
    triage = StubTriage(
        {
            101: Answer(matches_target=True, confidence=0.9, reason="stowaway hood, chest zip"),
            102: Answer(matches_target=False, confidence=0.8, reason="a fleece, not a shell"),
        }
    )

    result = await judge_over(transport, repo, db, triage=triage)

    by_id = {ranked.item.item_id: ranked for ranked in result.candidates}
    assert by_id[101].rank_score == 0.0, "the whole point: its title never names the model"
    assert by_id[102].rank_score == 1.0, "and this one's title names everything"
    assert order(result) == [101, 102]
    assert result.triaged == 2
    assert result.status == "ok"


async def test_confidence_ranks_but_never_drops_a_low_confidence_match(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """A 0.1-confidence match still reaches the verdict stage, just behind a 0.9 one.

    Filtering on confidence here would recreate exactly the failure R003 was written about:
    the model hedges on the listings whose photos are worst, and those are the bargains.
    """
    queue_page(
        transport,
        [make_item(item_id, photo_ts=PHOTO_TS, price="40.0") for item_id in (401, 402, 403)],
    )
    triage = StubTriage(
        {
            401: Answer(matches_target=True, confidence=0.1),
            402: Answer(matches_target=True, confidence=0.9),
            403: Answer(matches_target=False, confidence=0.95),
        }
    )

    result = await judge_over(transport, repo, db, triage=triage)

    assert order(result) == [402, 401, 403]
    assert len(result.candidates) == 3, "nothing was dropped for being a weak match"


async def test_an_untriaged_item_sits_below_a_match_and_above_a_rejection(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """The three-valued column, ordered. Never looked at is not the same as rejected."""
    queue_page(
        transport,
        [make_item(item_id, photo_ts=PHOTO_TS, price="40.0") for item_id in (501, 502, 503)],
    )
    triage = StubTriage(
        {501: Answer(matches_target=False, confidence=0.9), 503: Answer(matches_target=True)},
        skip={502},
    )

    result = await judge_over(transport, repo, db, triage=triage)

    assert order(result) == [503, 502, 501]
    by_id = {ranked.item.item_id: ranked for ranked in result.candidates}
    assert by_id[502].matches_target is None
    assert by_id[502].confidence is None
    assert await stage_of(db, 502) == "funnel", "no answer means no triage row was written"


async def test_ties_break_by_price_then_id_and_are_stable_across_two_runs(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Equal verdict, equal confidence, equal title score — and still one fixed order.

    "The top 3" only means something if it is the same three listings on a re-run, so the
    second sweep feeds the same listings back in the opposite order and has to agree.
    """
    entries = [
        make_item(301, photo_ts=PHOTO_TS, price="10.0"),
        make_item(302, photo_ts=PHOTO_TS, price="10.0"),
        make_item(303, photo_ts=PHOTO_TS, price="5.0"),
    ]
    same = {item_id: Answer(matches_target=True, confidence=0.5) for item_id in (301, 302, 303)}

    queue_page(transport, entries)
    first = await judge_over(transport, repo, db, triage=StubTriage(dict(same)))
    queue_page(transport, list(reversed(entries)))
    second = await judge_over(transport, repo, db, triage=StubTriage(dict(same)))

    assert order(first) == [303, 301, 302], "cheapest first, then the lower id"
    assert order(second) == order(first), "arrival order must not move anything"


# --- Batching -------------------------------------------------------------------------


async def test_every_funnel_survivor_is_triaged_in_batches_of_the_configured_size(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """45 candidates at 20 per batch is three requests carrying all 45 ids, none twice."""
    ids = list(range(600, 645))
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage({i: Answer(matches_target=i % 2 == 0) for i in ids})

    result = await judge_over(transport, repo, db, triage=triage, batch_size=20)

    assert [len(batch) for batch in triage.batches] == [20, 20, 5]
    sent = [item_id for batch in triage.batches for item_id in batch]
    assert sorted(sent) == ids
    assert len(sent) == len(set(sent)), "no listing was paid for twice"
    assert result.triaged == 45


async def test_the_target_is_built_once_and_carries_the_visual_signature(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Every batch judges against the same description of the thing being looked for."""
    ids = list(range(700, 725))
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage({})

    await judge_over(transport, repo, db, triage=triage, batch_size=10)

    assert len(triage.targets) == 3
    assert all(target.visual_signature == SIGNATURE for target in triage.targets)
    assert all(target.labels == LABELS for target in triage.targets)
    assert all(target.keywords == KEYWORDS for target in triage.targets)


# --- What the flow got wrong ----------------------------------------------------------


async def test_an_invented_id_and_a_skipped_id_are_both_logged_and_neither_reorders(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """A flow answering about a listing nobody sent must not touch the ranking.

    Both directions are counted because both are silent: an extra id would otherwise write
    a verdict onto some other batch's candidate, and a missing one would look exactly like
    a listing the model was never given.
    """
    ids = [801, 802, 803]
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage(
        {801: Answer(matches_target=True, confidence=0.9), 803: Answer(matches_target=False)},
        invent={0: [999_999]},
        skip={802},
    )

    with structlog.testing.capture_logs() as entries:
        result = await judge_over(transport, repo, db, triage=triage)

    mismatch = [entry for entry in entries if entry["event"] == "magic.triage_mismatch"]
    assert len(mismatch) == 1, "one line per batch, not one per id"
    assert mismatch[0]["sent"] == 3
    assert mismatch[0]["returned"] == 3
    assert mismatch[0]["unknown"] == [999_999]
    assert mismatch[0]["missing"] == [802]

    assert order(result) == [801, 802, 803]
    assert result.triaged == 2, "the invented id is not a candidate and was not counted"
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM sweep_candidates WHERE item_id = ?", (999_999,)
    )
    assert int(row["n"]) == 0, "an id nobody sent must never reach the sweep tables"


async def test_an_invented_id_never_overwrites_another_batchs_answer(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """The reconcile is per batch, so batch two cannot rewrite batch one's paid-for verdict.

    `record_triage` matches on `(sweep_id, item_id)`, so an id echoed back by the wrong
    batch would land on a real row — which is why the caller filters to the ids it sent
    rather than trusting the flow's list.
    """
    ids = [901, 902, 903, 904]
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage(
        {item_id: Answer(matches_target=True, confidence=0.9) for item_id in ids},
        # Batch two answers about batch one's first listing, claiming a rejection.
        invent={1: [901]},
    )

    result = await judge_over(transport, repo, db, triage=triage, batch_size=2)

    by_id = {ranked.item.item_id: ranked for ranked in result.candidates}
    assert by_id[901].matches_target is True
    assert by_id[901].confidence == 0.9
    row = await db.fetch_one(
        "SELECT matches_target, confidence FROM sweep_candidates WHERE item_id = ?", (901,)
    )
    assert int(row["matches_target"]) == 1


# --- Failure ---------------------------------------------------------------------------


async def test_a_failing_batch_closes_the_run_partial_and_keeps_what_was_paid_for(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Batch two dies; batch one's twenty answers stay on disk and nothing raises."""
    ids = list(range(1000, 1045))
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage({i: Answer(matches_target=True) for i in ids}, fail_on=1)

    result = await judge_over(transport, repo, db, triage=triage, batch_size=20)

    assert result.status == "partial"
    assert result.error is not None and "502" in result.error
    assert result.triaged == 20, "the batch that succeeded is still counted"
    assert len(triage.batches) == 2, "it stopped rather than carrying on to batch three"
    assert len(result.candidates) == 45, "candidates already stored are never discarded"

    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.status == "partial"
    assert run.candidates == 45
    triaged_rows = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM sweep_candidates WHERE stage = 'triage' AND sweep_id = ?",
        (result.sweep_id,),
    )
    assert int(triaged_rows["n"]) == 20


async def test_a_blocked_sweep_never_reaches_the_photo_check(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """A run that could not read the site must not then go and spend money on it."""
    transport.queue_status(403)
    triage = StubTriage({})

    result = await judge_over(transport, repo, db, triage=triage)

    assert result.status == "blocked"
    assert triage.batches == [], "not one request was paid for"
    assert result.triaged == 0
    assert result.cost_eur == 0.0


async def test_a_partial_sweep_never_reaches_the_photo_check(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Same rule for the softer failures: an incomplete read is not a shopping list."""
    transport.queue_status(500)
    triage = StubTriage({})

    result = await judge_over(transport, repo, db, triage=triage)

    assert result.status == "partial"
    assert triage.batches == []


async def test_a_sweep_that_found_nothing_makes_no_request(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """An empty batch is a real cost with a guaranteed empty answer."""
    transport.queue_catalog([])
    triage = StubTriage({})

    result = await judge_over(transport, repo, db, triage=triage)

    assert result.status == "ok"
    assert result.candidates == []
    assert triage.batches == []


# --- Cost -------------------------------------------------------------------------------


async def test_the_flows_own_price_wins_over_the_configured_rates(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """A flow that prices its own call is believed; the rates are only a fallback."""
    ids = list(range(1100, 1104))
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage({}, usage=Usage(input_tokens=1000, output_tokens=200, cost_eur=0.25))

    result = await judge_over(transport, repo, db, triage=triage, batch_size=2)

    assert result.tokens == 2400, "two batches, both counted"
    assert result.cost_eur == 0.5
    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.tokens == 2400
    assert run.cost_eur == 0.5


async def test_a_flow_that_reports_nothing_is_not_an_error(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """No usage block means no cost line, not a failed sweep."""
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in (1201, 1202)])

    result = await judge_over(transport, repo, db, triage=StubTriage({}), batch_size=2)

    assert result.status == "ok"
    assert result.tokens == 0
    assert result.cost_eur == 0.0
    assert result.triaged == 2


# --- Isolation still holds with the AI stages on top --------------------------------------


async def test_a_judged_sweep_still_writes_only_to_the_sweep_tables(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """The slice's safety claim, re-asserted with triage in the loop.

    `test_sweep_isolation.py` pins `run_sweep`. This pins the stage that was added on top of
    it, because "judged" is the path that will actually run in production.
    """
    ids = list(range(1300, 1325))
    queue_page(transport, [make_item(i, photo_ts=PHOTO_TS, price="40.0") for i in ids])
    triage = StubTriage({i: Answer(matches_target=True, confidence=0.7) for i in ids})

    result = await judge_over(transport, repo, db, triage=triage, batch_size=10)

    assert result.triaged == 25, "the empty counts below mean nothing without real work"
    for table in ("items", "outbox", "market"):
        row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
        assert int(row["n"]) == 0, f"a judged sweep wrote to {table}"
