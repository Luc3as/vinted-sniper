"""Sweep storage: its own tables, ordered best-first, and cleaned up with its run."""

from __future__ import annotations

import pytest

from vinted_sniper.db.connection import Database
from vinted_sniper.db.repo import Repo, SweepCandidate


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
