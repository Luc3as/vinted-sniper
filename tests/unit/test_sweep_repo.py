"""Sweep storage: its own tables, ordered best-first, and cleaned up with its run."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from vinted_sniper.db.connection import Database
from vinted_sniper.db.repo import Repo, SweepCandidate
from vinted_sniper.enrichment import EnrichmentIn


def make_candidate(item_id: int, *, rank_score: float = 0.0, **overrides: object) -> SweepCandidate:
    fields: dict[str, object] = {
        "item_id": item_id,
        "title": f"Patagonia Torrentshell {item_id}",
        "url": f"https://www.vinted.sk/items/{item_id}",
        "rank_score": rank_score,
        "price": 45.0,
        "total_price": 50.2,
        "currency": "EUR",
        "brand": "Patagonia",
        "size": "M",
        "condition": "Very good",
        "photo_url": f"https://images.vinted.net/{item_id}.jpeg",
        "photo_urls": [f"https://images.vinted.net/{item_id}.jpeg"],
        "seller_login": "seller",
    }
    fields.update(overrides)
    return SweepCandidate(**fields)  # type: ignore[arg-type]


async def test_a_sweep_without_a_saved_search_round_trips(repo: Repo) -> None:
    """A sweep runs before anyone decides to keep the search, so query_id may be None."""
    sweep_id = await repo.create_sweep_run(
        tld="sk",
        params={"search_text": "torrentshell", "order": "relevance"},
        keywords=["torrentshell", "patagonia"],
    )

    run = await repo.get_sweep_run(sweep_id)

    assert run is not None
    assert run.query_id is None
    assert run.status == "running"
    assert run.tld == "sk"
    assert run.params == {"search_text": "torrentshell", "order": "relevance"}
    assert run.keywords == ["torrentshell", "patagonia"]
    assert run.started_at > 0
    assert run.finished_at is None
    assert (run.pages_fetched, run.items_seen, run.candidates) == (0, 0, 0)
    assert run.funnel == {}


async def test_a_sweep_can_be_attached_to_a_saved_search(repo: Repo) -> None:
    query_id = await repo.add_query(
        name="torrentshell",
        url="https://www.vinted.sk/catalog?search_text=torrentshell",
        tld="sk",
        params={"search_text": "torrentshell"},
        poll_interval_s=60,
    )

    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[], query_id=query_id)

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None and run.query_id == query_id


async def test_candidates_come_back_best_first(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=["torrentshell"])
    # Handed over in a deliberately unsorted order: rank, not arrival, decides.
    written = await repo.record_sweep_candidates(
        sweep_id,
        [
            make_candidate(11, rank_score=1.5),
            make_candidate(22, rank_score=9.0),
            make_candidate(33, rank_score=4.0),
        ],
    )

    stored = await repo.sweep_candidates(sweep_id)

    assert written == 3
    assert [c.item_id for c in stored] == [22, 33, 11]
    assert [c.position for c in stored] == [1, 2, 0]
    best = stored[0]
    assert best.rank_score == 9.0
    assert best.stage == "funnel"
    assert best.reason is None
    assert best.title == "Patagonia Torrentshell 22"
    assert best.price == 45.0
    assert best.total_price == 50.2
    assert best.currency == "EUR"
    assert best.photo_urls == ["https://images.vinted.net/22.jpeg"]
    assert best.promoted is False
    assert best.sweep_id == sweep_id


async def test_equal_ranks_fall_back_to_the_order_they_arrived_in(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(
        sweep_id, [make_candidate(7), make_candidate(8), make_candidate(9)]
    )

    assert [c.item_id for c in await repo.sweep_candidates(sweep_id)] == [7, 8, 9]


async def test_finishing_a_sweep_persists_status_and_funnel(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])

    await repo.finish_sweep_run(
        sweep_id,
        status="done",
        pages_fetched=3,
        items_seen=89,
        candidates=6,
        funnel={"fetched": 89, "priced": 41, "ranked": 6},
    )

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None
    assert run.status == "done"
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert (run.pages_fetched, run.items_seen, run.candidates) == (3, 89, 6)
    assert run.funnel == {"fetched": 89, "priced": 41, "ranked": 6}
    assert run.error is None


async def test_a_failed_sweep_keeps_its_error_and_partial_counts(repo: Repo) -> None:
    """A sweep that dies partway is still a row worth reading, not a hole."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])

    await repo.finish_sweep_run(
        sweep_id,
        status="failed",
        pages_fetched=1,
        items_seen=20,
        candidates=0,
        funnel={"fetched": 20},
        error="x" * 900,
    )

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None
    assert run.status == "failed"
    assert run.pages_fetched == 1
    assert run.error is not None and len(run.error) == 500


async def test_deleting_a_run_takes_its_candidates_with_it(repo: Repo, db: Database) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1), make_candidate(2)])

    await db.execute("DELETE FROM sweep_runs WHERE id = ?", (sweep_id,))

    assert await repo.get_sweep_run(sweep_id) is None
    assert await repo.sweep_candidates(sweep_id) == []


async def test_deleting_the_search_keeps_the_sweep(repo: Repo, db: Database) -> None:
    """The sweep is the evidence for keeping the search; deleting the search
    must not erase it."""
    query_id = await repo.add_query(
        name="torrentshell",
        url="https://www.vinted.sk/catalog?search_text=torrentshell",
        tld="sk",
        params={"search_text": "torrentshell"},
        poll_interval_s=60,
    )
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[], query_id=query_id)

    await db.execute("DELETE FROM queries WHERE id = ?", (query_id,))

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None and run.query_id is None


async def test_reading_a_sweep_that_never_existed(repo: Repo) -> None:
    assert await repo.get_sweep_run(4242) is None
    assert await repo.sweep_candidates(4242) == []


async def test_writing_no_candidates_is_a_no_op(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])

    assert await repo.record_sweep_candidates(sweep_id, []) == 0
    assert await repo.sweep_candidates(sweep_id) == []


async def test_the_same_listing_twice_in_one_sweep_updates_rather_than_duplicates(
    repo: Repo,
) -> None:
    """S03 re-writes a candidate once triage has judged it; that must not duplicate rows."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(5, rank_score=1.0)])

    await repo.record_sweep_candidates(
        sweep_id,
        [make_candidate(5, rank_score=8.0, stage="triage", reason="thumbnail matches")],
    )

    stored = await repo.sweep_candidates(sweep_id)
    assert len(stored) == 1
    assert stored[0].rank_score == 8.0
    assert stored[0].stage == "triage"
    assert stored[0].reason == "thumbnail matches"


async def test_a_candidate_for_a_sweep_that_does_not_exist_is_refused(repo: Repo) -> None:
    """Foreign keys are on, so orphan candidates cannot be written at all."""
    with pytest.raises(Exception, match="FOREIGN KEY constraint failed"):
        await repo.record_sweep_candidates(9999, [make_candidate(1)])


async def test_sweeps_never_touch_the_pollers_item_table(repo: Repo, db: Database) -> None:
    """The whole point of separate tables: a swept listing stays unknown to the poller."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1234)])

    assert await repo.known_item_ids([1234]) == set()
    assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0
    assert await db.fetch_value("SELECT COUNT(*) FROM outbox") == 0


@dataclass(frozen=True)
class FakeTriage:
    """The shape `magic.triage` returns: id, judgement, confidence, one-line reason."""

    id: int
    matches_target: bool
    confidence: float
    reason: str | None = None


async def test_triage_lands_on_the_right_rows_and_leaves_the_rest_alone(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(
        sweep_id, [make_candidate(1), make_candidate(2), make_candidate(3)]
    )

    written = await repo.record_triage(
        sweep_id,
        [
            FakeTriage(id=1, matches_target=True, confidence=0.9, reason="right shell, right cut"),
            FakeTriage(id=3, matches_target=False, confidence=0.2, reason="a fleece, not a shell"),
        ],
    )

    assert written == 2
    stored = {c.item_id: c for c in await repo.sweep_candidates(sweep_id)}
    assert stored[1].stage == "triage"
    assert stored[1].matches_target is True
    assert stored[1].confidence == 0.9
    assert stored[1].triage_reason == "right shell, right cut"
    assert stored[3].matches_target is False
    assert stored[3].confidence == 0.2
    # Untouched: never triaged is not the same answer as triaged and rejected.
    assert stored[2].stage == "funnel"
    assert stored[2].matches_target is None
    assert stored[2].confidence is None
    assert stored[2].triage_reason is None


async def test_triage_for_an_item_outside_the_sweep_is_a_no_op(repo: Repo) -> None:
    """A flow that invents an id must not take the rest of the batch down with it."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1)])

    written = await repo.record_triage(
        sweep_id,
        [
            FakeTriage(id=1, matches_target=True, confidence=0.8),
            FakeTriage(id=404, matches_target=True, confidence=0.7),
        ],
    )

    # It reports what it was handed; the caller compares that against the ids it sent.
    assert written == 2
    stored = await repo.sweep_candidates(sweep_id)
    assert [c.item_id for c in stored] == [1]
    assert stored[0].matches_target is True


async def test_triaging_nothing_is_a_no_op(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1)])

    assert await repo.record_triage(sweep_id, []) == 0
    assert (await repo.sweep_candidates(sweep_id))[0].stage == "funnel"


async def test_re_running_the_funnel_write_never_clears_a_triage_result(repo: Repo) -> None:
    """Triage costs money; writing the funnel again must not erase what it bought."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(5, rank_score=1.0)])
    await repo.record_triage(
        sweep_id, [FakeTriage(id=5, matches_target=True, confidence=0.77, reason="same jacket")]
    )

    await repo.record_sweep_candidates(sweep_id, [make_candidate(5, rank_score=2.0)])

    stored = await repo.sweep_candidates(sweep_id)
    assert len(stored) == 1
    assert stored[0].rank_score == 2.0
    assert stored[0].matches_target is True
    assert stored[0].confidence == 0.77
    assert stored[0].triage_reason == "same jacket"


async def test_a_thumbnail_url_round_trips(repo: Repo) -> None:
    """The small variant is what the triage stage is billed on, so it must be stored."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(
        sweep_id, [make_candidate(1, thumb_url="https://images.vinted.net/1-thumb.jpeg")]
    )

    stored = await repo.sweep_candidates(sweep_id)
    assert stored[0].thumb_url == "https://images.vinted.net/1-thumb.jpeg"
    assert stored[0].photo_url == "https://images.vinted.net/1.jpeg"


async def test_a_verdict_is_stored_on_the_candidate_not_on_items(repo: Repo, db: Database) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1), make_candidate(2)])

    wrote = await repo.record_verdict(
        sweep_id,
        1,
        EnrichmentIn(
            score=88,
            model="Patagonia Torrentshell 3L",
            retail_price=Decimal("180.00"),
            retail_source="patagonia.com",
            matches_query=True,
            risk="no authenticity worries",
            verdict="Half price for the current model.",
        ),
        judged_at=1_700_000_000,
    )

    assert wrote is True
    stored = {c.item_id: c for c in await repo.sweep_candidates(sweep_id)}
    judged = stored[1]
    assert judged.stage == "verdict"
    assert judged.verdict_score == 88
    assert judged.verdict_model == "Patagonia Torrentshell 3L"
    assert judged.verdict_retail_price == 180.0
    assert judged.verdict_retail_source == "patagonia.com"
    assert judged.verdict_matches_query is True
    assert judged.verdict_risk == "no authenticity worries"
    assert judged.verdict_text == "Half price for the current model."
    assert judged.judged_at == 1_700_000_000
    assert stored[2].judged_at is None
    # The whole point of the separate columns: no items row was created for a candidate.
    assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0


async def test_a_partial_verdict_stores_what_it_has(repo: Repo) -> None:
    """Everything in EnrichmentIn is optional, and a partial answer beats none."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1)])

    assert await repo.record_verdict(
        sweep_id, 1, EnrichmentIn(verdict="Looks right but the photos are dark."), judged_at=17
    )

    stored = (await repo.sweep_candidates(sweep_id))[0]
    assert stored.stage == "verdict"
    assert stored.verdict_text == "Looks right but the photos are dark."
    assert stored.verdict_score is None
    assert stored.verdict_matches_query is None
    assert stored.judged_at == 17


async def test_a_verdict_for_an_item_outside_the_sweep_reports_that_it_missed(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1)])

    assert await repo.record_verdict(sweep_id, 404, EnrichmentIn(score=50), judged_at=17) is False
    assert await repo.record_verdict(4242, 1, EnrichmentIn(score=50), judged_at=17) is False


async def test_a_verdict_never_clears_the_triage_answer_underneath_it(repo: Repo) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    await repo.record_sweep_candidates(sweep_id, [make_candidate(1)])
    await repo.record_triage(
        sweep_id, [FakeTriage(id=1, matches_target=True, confidence=0.9, reason="same jacket")]
    )

    await repo.record_verdict(sweep_id, 1, EnrichmentIn(score=70), judged_at=17)

    stored = (await repo.sweep_candidates(sweep_id))[0]
    assert stored.matches_target is True
    assert stored.confidence == 0.9
    assert stored.triage_reason == "same jacket"


async def test_sweep_cost_accumulates_across_stages(repo: Repo) -> None:
    """A run that dies after triage still reports what triage cost, so the writer adds."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])

    await repo.add_sweep_cost(sweep_id, 12_000, 0.04)
    await repo.add_sweep_cost(sweep_id, 14_600, 0.06)

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None
    assert run.tokens == 26_600
    assert run.cost_eur == pytest.approx(0.10)


async def test_adding_cost_to_a_sweep_that_does_not_exist_is_harmless(repo: Repo) -> None:
    await repo.add_sweep_cost(4242, 100, 1.0)

    assert await repo.get_sweep_run(4242) is None
