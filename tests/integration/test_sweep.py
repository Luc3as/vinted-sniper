"""Paging a sweep over the real client, and stopping at the right moment.

These run against a real `SessionManager`, `VintedClient` and `Repo` over a scripted
transport, because the things worth asserting are exactly the ones a mock would let you
get wrong: that every catalog request carried `order=relevance`, that the fifth page was
never asked for, and that a refusal left the shared site cooldown in the state the standing
pollers expect.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import ScriptedTransport
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import sweep
from vinted_sniper.vinted.client import PER_PAGE, VintedClient
from vinted_sniper.vinted.session import SessionManager

PHOTO_TS = 1_760_000_000

PARAMS = {"search_text": "torrentshell", "order": "newest_first"}
KEYWORDS = ["patagonia", "torrentshell"]


def client_for(transport: ScriptedTransport, db: Any) -> tuple[VintedClient, SessionManager]:
    """The same client the poller runs on, over a transport the test scripts."""
    sessions = SessionManager(db, transport)
    return VintedClient(transport, sessions), sessions


def catalog_requests(transport: ScriptedTransport) -> list[dict[str, Any]]:
    """Only the catalog calls; the session bootstrap hits the homepage first."""
    return [req for req in transport.requests if "/api/v2/" in req["url"]]


def queue_page(
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
    ids: range | list[int],
) -> None:
    transport.queue_catalog([make_item(i, photo_ts=PHOTO_TS) for i in ids])


async def sweep_over(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    *,
    max_pages: int = 4,
    max_items: int = 1000,
    sessions: SessionManager | None = None,
    params: dict[str, str] | None = None,
) -> sweep.SweepResult:
    client, made_sessions = client_for(transport, db)
    return await sweep.run_sweep(
        tld="fr",
        params=PARAMS if params is None else params,
        keywords=KEYWORDS,
        client=client,
        repo=repo,
        max_pages=max_pages,
        max_items=max_items,
        sessions=sessions if sessions is not None else made_sessions,
    )


# --- Relevance ordering and the page ceiling ----------------------------------------


async def test_paging_is_relevance_ordered_and_stops_at_the_page_ceiling(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Four full pages is four requests, in relevance order, and never a fifth.

    The ceiling is what keeps a sweep's cost knowable, so the sixth queued page being
    ignored is the assertion that matters most here.
    """
    for page in range(6):
        queue_page(transport, make_item, range(page * PER_PAGE, (page + 1) * PER_PAGE))

    result = await sweep_over(transport, repo, db, max_pages=4)

    requests = catalog_requests(transport)
    assert len(requests) == 4, "the page ceiling stopped paging, not the queue running dry"
    assert [req["params"]["page"] for req in requests] == ["1", "2", "3", "4"]
    assert all(req["params"]["order"] == "relevance" for req in requests)
    assert result.pages_fetched == 4
    assert result.status == "ok"


async def test_the_relevance_override_never_touches_the_callers_params(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """R-invariant: the stored search keeps saying newest_first.

    `params` is usually a saved query's own dict. If a sweep mutated it, the next poll of
    that search would silently run in relevance order and its dedup would stop meaning
    anything.
    """
    stored = {"search_text": "torrentshell", "order": "newest_first"}
    queue_page(transport, make_item, range(3))

    await sweep_over(transport, repo, db, params=stored)

    assert stored == {"search_text": "torrentshell", "order": "newest_first"}
    assert "page" not in stored


# --- Early stop -----------------------------------------------------------------------


async def test_a_short_page_ends_the_sweep(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Fewer than 96 results means that was the last page.

    `_parse_catalog` drops the response's pagination block, so a short page is the only
    end-of-results signal a sweep has.
    """
    queue_page(transport, make_item, range(PER_PAGE))
    queue_page(transport, make_item, range(PER_PAGE, PER_PAGE + 10))
    queue_page(transport, make_item, range(1000, 1000 + PER_PAGE))

    result = await sweep_over(transport, repo, db, max_pages=4)

    assert len(catalog_requests(transport)) == 2, "no third request after a short page"
    assert result.pages_fetched == 2
    assert result.items_seen == PER_PAGE + 10


async def test_the_item_ceiling_stops_paging_and_caps_what_is_stored(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """The item ceiling is the AI budget, so it bounds both paging and storage."""
    for page in range(4):
        queue_page(transport, make_item, range(page * PER_PAGE, (page + 1) * PER_PAGE))

    result = await sweep_over(transport, repo, db, max_pages=4, max_items=100)

    assert len(catalog_requests(transport)) == 2, "page 2 crossed the ceiling; page 3 is moot"
    assert len(result.candidates) == 100
    assert len(await repo.sweep_candidates(result.sweep_id)) == 100


async def test_a_listing_on_two_pages_is_stored_once(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """Relevance paging is not a stable window; the same id can surface twice."""
    queue_page(transport, make_item, range(PER_PAGE))
    # Page two repeats id 95 as its first entry, as a shifting ranking does.
    queue_page(transport, make_item, [PER_PAGE - 1, *range(PER_PAGE, PER_PAGE + 5)])

    result = await sweep_over(transport, repo, db, max_pages=4)

    assert result.items_seen == PER_PAGE + 5, "the repeat was collapsed, not counted twice"
    stored = await repo.sweep_candidates(result.sweep_id)
    ids = [candidate.item_id for candidate in stored]
    assert len(ids) == len(set(ids))
    assert ids.count(PER_PAGE - 1) == 1


# --- Failure paths --------------------------------------------------------------------


async def test_a_block_mid_sweep_keeps_the_pages_already_read(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """A refused sweep is a recorded partial result, never an exception.

    It must also leave the shared site cooldown closed: a sweep and the standing pollers
    go out through one address, and a refusal nobody wrote down is one every poller then
    walks straight into.
    """
    queue_page(transport, make_item, range(PER_PAGE))
    transport.queue_status(403)

    client, sessions = client_for(transport, db)
    result = await sweep.run_sweep(
        tld="fr",
        params=PARAMS,
        keywords=KEYWORDS,
        client=client,
        repo=repo,
        max_pages=4,
        max_items=1000,
        sessions=sessions,
    )

    assert result.status == "blocked"
    assert result.error
    assert result.pages_fetched == 1, "page one survived the refusal on page two"
    assert len(await repo.sweep_candidates(result.sweep_id)) == PER_PAGE

    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.status == "blocked"
    assert run.finished_at is not None
    assert run.pages_fetched == 1

    assert sessions.cooldown.is_closed("fr"), "the whole site is held, not just this sweep"
    held_until = await repo.get_state_value("cooldown_until:fr")
    assert held_until is not None and int(held_until) > int(time.time())


@pytest.mark.parametrize(
    "status_code",
    [
        401,  # AuthExpiredError - the anonymous token aged out
        429,  # RateLimitedError
        500,  # NetworkError - upstream fell over
    ],
)
async def test_transport_failures_finish_the_run_as_partial(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
    status_code: int,
) -> None:
    """Every typed Vinted error ends the sweep the same way: partial, recorded, no raise."""
    queue_page(transport, make_item, range(PER_PAGE))
    transport.queue_status(status_code)

    result = await sweep_over(transport, repo, db, max_pages=4)

    assert result.status == "partial"
    assert result.error
    assert result.pages_fetched == 1
    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.status == "partial"
    assert run.error


async def test_a_non_json_body_is_a_partial_run_not_a_crash(
    transport: ScriptedTransport, repo: Repo, db: Any, make_item: Callable[..., dict[str, Any]]
) -> None:
    """An anti-bot interstitial served with a 200 is the common shape of this."""
    queue_page(transport, make_item, range(PER_PAGE))
    transport.queue_status(200, "<html>are you a robot</html>")

    result = await sweep_over(transport, repo, db, max_pages=4)

    assert result.status == "partial"
    assert result.pages_fetched == 1
    assert len(await repo.sweep_candidates(result.sweep_id)) == PER_PAGE


async def test_a_sweep_that_fails_on_page_one_still_leaves_a_closed_run(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    """The sweep_runs row exists from before the first request, so it must be closed too."""
    transport.queue_status(429)

    result = await sweep_over(transport, repo, db, max_pages=4)

    assert result.pages_fetched == 0
    assert result.candidates == []
    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.status == "partial"
    assert run.finished_at is not None
    assert run.candidates == 0


async def test_run_sweep_is_awaitable_without_a_session_manager(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    """`sessions` is optional; without it a block is recorded but no cooldown is applied."""
    transport.queue_status(403)
    client, _ = client_for(transport, db)

    result = await asyncio.wait_for(
        sweep.run_sweep(
            tld="fr",
            params=PARAMS,
            keywords=KEYWORDS,
            client=client,
            repo=repo,
            max_pages=2,
            max_items=10,
        ),
        timeout=5,
    )

    assert result.status == "blocked"
