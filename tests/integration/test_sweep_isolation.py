"""The sweep's write path, pinned to the four tables it is allowed to touch.

"No Telegram alerts fired" is this slice's headline safety claim, and a claim that only
lives in a docstring cannot fail a build. Alerts are `outbox` rows, written by
`Repo.record_new_items()` and `Repo.record_price_drops()` and claimed by the dispatcher —
so an empty `outbox` after a real multi-page sweep is the assertion that makes the claim
enforceable.

Two quieter hazards are pinned alongside it, because neither would announce itself:

* A row in `items` is silent data loss, not noise. `Repo.known_item_ids()` queries `items`
  globally with no `query_id` filter, so a listing a sweep wrote there is one the standing
  poller treats as already-seen and never alerts on again.
* A row in `market` is keyed `(item_id, query_id)`, so a sweep writing there would fold its
  own reading into a standing search's price history and skew the percentile the search's
  own gates are judged against.
* `store_enrichment()` on a sweep candidate is a no-op that returns `False`. Nothing
  raises, nothing is written, and the expensive verdict is simply lost — so the guard below
  covers the two `magic` clients as well as the engine, `verdict.py` being where the
  temptation to reach for it actually lives.

The counts are only meaningful next to a non-empty `sweep_candidates`: an empty database is
trivially isolated, so every test here first proves the sweep did real work.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.conftest import ScriptedTransport
from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import sweep
from vinted_sniper.magic import triage as magic_triage
from vinted_sniper.magic import verdict as magic_verdict
from vinted_sniper.magic.models import TriageBatch, TriageItem
from vinted_sniper.vinted.client import PER_PAGE, VintedClient
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.web import server as web_server
from vinted_sniper.web.server import SESSION_COOKIE, create_app

PHOTO_TS = 1_760_000_000

# The standing watch this sweep runs alongside: same site, same search text, so nothing but
# the write path itself separates the two.
WATCH_URL = "https://www.vinted.fr/catalog?search_text=torrentshell"
PARAMS = {"search_text": "torrentshell", "order": "newest_first"}
KEYWORDS = ["patagonia", "torrentshell"]

# The three writers a sweep must never reach: the two that produce outbox rows and the one
# that writes a standing search's price history. Named here so a rename that reintroduces
# the hazard fails this test rather than shipping quietly.
ALERT_WRITERS = ("record_new_items", "record_price_drops", "observe_market")

# Plus the one that is not an alert at all and is worse for it. `store_enrichment()` is
# `UPDATE items ... WHERE item_id = ?`, so a sweep candidate — which has no row in `items`
# and must never gain one — matches nothing and the call returns `False`. Nothing raises,
# nothing is stored, and the verdict an operator paid for is gone. `record_verdict()` is
# the sweep's writer; this is the guard that keeps the wrong one out.
FORBIDDEN_WRITERS = (*ALERT_WRITERS, "store_enrichment")

# Read from the imported modules rather than from paths, so the source-level guards below
# cannot be silently skipped by running pytest from a different working directory. This
# is the same seam `tests/unit/test_cli_sweep.py` uses on `_cmd_sweep`.
SWEEP_SOURCE = inspect.getsource(sweep)

# Every module on the sweep's write path. `engine/sweep.py` orchestrates it, and the two
# `magic` clients are where the answers arrive — `verdict.py` most of all, because it holds
# an enrichment answer in its hand and `store_enrichment()` is the obvious-looking place to
# put it.
# Plus the sweep's newest entry point. S04 lets the browser start a sweep, and a launcher
# living in `web/server.py` is outside the three modules above — so the guard would go on
# passing while the hazard moved somewhere it no longer looked.
#
# Scoped to the launcher function rather than the whole module on purpose: `web/server.py`
# legitimately calls `store_enrichment()` for the enrichment ingest endpoint, which is a
# standing-poller concern and has nothing to do with a sweep. Scanning the module would
# make this entry permanently red for an innocent reason; scanning `_run_magic_sweep()`
# keeps it a real guard over the code that actually runs on a sweep's behalf.
GUARDED_MODULES = {
    "engine/sweep.py": SWEEP_SOURCE,
    "magic/triage.py": inspect.getsource(magic_triage),
    "magic/verdict.py": inspect.getsource(magic_verdict),
    "web/server.py::_run_magic_sweep": inspect.getsource(web_server._run_magic_sweep),
}


async def count(db: Any, table: str) -> int:
    row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"])


async def seed_standing_watch(repo: Repo) -> int:
    """A genuine saved search with state already on it, as a running deployment has."""
    query_id = await repo.add_query(
        name="torrentshell watch",
        url=WATCH_URL,
        tld="fr",
        params=dict(PARAMS),
        poll_interval_s=60,
    )
    # Pre-sweep state that is not all defaults, so "untouched" is a real comparison rather
    # than a comparison against zeroes the sweep could have written itself.
    await repo.record_success(
        query_id,
        newest_raw_ts=PHOTO_TS,
        newest_item_ts=PHOTO_TS,
        seen=7,
        stale_cycles=0,
    )
    return query_id


async def run_full_sweep(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    *,
    query_id: int | None = None,
    max_pages: int = 3,
) -> sweep.SweepResult:
    sessions = SessionManager(db, transport)
    client = VintedClient(transport, sessions)
    return await sweep.run_sweep(
        tld="fr",
        params=PARAMS,
        keywords=KEYWORDS,
        client=client,
        repo=repo,
        max_pages=max_pages,
        max_items=1000,
        query_id=query_id,
        sessions=sessions,
    )


def queue_pages(
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
    pages: int,
    *,
    last_page_size: int = 5,
) -> None:
    """Full pages, then a short one, so the sweep ends by reading everything there was."""
    for page in range(pages - 1):
        transport.queue_catalog(
            [
                make_item(item_id, photo_ts=PHOTO_TS)
                for item_id in range(page * PER_PAGE, (page + 1) * PER_PAGE)
            ]
        )
    start = (pages - 1) * PER_PAGE
    transport.queue_catalog(
        [make_item(i, photo_ts=PHOTO_TS) for i in range(start, start + last_page_size)]
    )


# --- The write path -------------------------------------------------------------------


async def test_a_multi_page_sweep_writes_only_to_the_sweep_tables(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """The whole safety claim in one run: real work in, nothing in the poller's tables."""
    query_id = await seed_standing_watch(repo)
    queue_pages(transport, make_item, pages=3)

    result = await run_full_sweep(transport, repo, db, query_id=query_id)

    assert result.status == "ok"
    assert result.pages_fetched == 3, "a real multi-page read, not one short page"
    stored = await count(db, "sweep_candidates")
    assert stored > 0, "the empty counts below mean nothing unless the sweep stored something"
    assert stored == len(result.candidates)

    assert await count(db, "items") == 0, (
        "a sweep row in `items` makes known_item_ids() hide that listing from every poller"
    )
    assert await count(db, "outbox") == 0, "an outbox row is a Telegram alert"
    assert await count(db, "market") == 0, "market is keyed (item_id, query_id) and would skew"


async def test_the_standing_watchs_state_row_is_untouched(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """Same params, same site, and the watch's own bookkeeping does not move.

    `newest_item_ts` is the poller's "everything older than this is old news" mark. A sweep
    that advanced it would skip every arrival in between on the next tick.
    """
    query_id = await seed_standing_watch(repo)
    before = await repo.get_state(query_id)
    queue_pages(transport, make_item, pages=2)

    await run_full_sweep(transport, repo, db, query_id=query_id, max_pages=2)

    after = await repo.get_state(query_id)
    assert after == before
    assert after.newest_item_ts == PHOTO_TS
    assert after.items_seen_total == 7
    assert after.last_polled_at == before.last_polled_at


async def test_the_dispatchers_wakeup_event_is_never_set(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """Nothing in a sweep reaches the event the poller uses to wake the dispatcher.

    The poller sets `work_available` after writing outbox rows. `run_sweep` has no parameter
    to receive it, which is the structural half of this guarantee; the assertion is the
    behavioural half, and it would start failing the moment one were added and used.
    """
    work_available = asyncio.Event()
    query_id = await seed_standing_watch(repo)
    queue_pages(transport, make_item, pages=2)

    result = await run_full_sweep(transport, repo, db, query_id=query_id, max_pages=2)

    assert len(result.candidates) > 0
    assert not work_available.is_set()
    assert "work_available" not in SWEEP_SOURCE


@pytest.mark.parametrize("module", sorted(GUARDED_MODULES))
@pytest.mark.parametrize("forbidden", FORBIDDEN_WRITERS)
def test_no_sweep_module_names_a_poller_writer(module: str, forbidden: str) -> None:
    """A source-level guard, so the hazard is caught at edit time, not at run time.

    Matching on parsed `Name`/`Attribute` nodes rather than on raw text means the module
    docstrings and the comments above may keep naming these methods — explaining why a
    sweep must not call them is the point — without the explanation tripping its own guard.
    """
    tree = ast.parse(GUARDED_MODULES[module])
    referenced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    assert forbidden not in referenced, (
        f"{module} references {forbidden}; that is the poller's own write path, and on a "
        "sweep candidate it writes nothing at all"
    )


# --- Isolation under failure ----------------------------------------------------------


async def test_a_blocked_sweep_writes_nothing_to_the_poller_tables_either(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """The partial path is the one that would be written carelessly, so it is pinned too.

    A refusal mid-sweep still funnels and stores the pages it managed to read; that storage
    must land in the same two tables the happy path uses.
    """
    query_id = await seed_standing_watch(repo)
    transport.queue_catalog([make_item(i, photo_ts=PHOTO_TS) for i in range(PER_PAGE)])
    transport.queue_status(403)

    result = await run_full_sweep(transport, repo, db, query_id=query_id)

    assert result.status == "blocked"
    assert await count(db, "sweep_candidates") == PER_PAGE, "page one was kept"
    assert await count(db, "items") == 0
    assert await count(db, "outbox") == 0
    assert await count(db, "market") == 0
    # The refusal is written to the shared cooldown, not to the watch's own failure counters.
    assert (await repo.get_state(query_id)).count_403 == 0


async def test_deleting_the_standing_watch_leaves_the_sweep_evidence(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """Isolation runs the other way too: the watch's lifecycle does not delete sweep rows.

    `sweep_runs.query_id` is ON DELETE SET NULL by design — the sweep that justified a
    search must outlive the search itself.
    """
    query_id = await seed_standing_watch(repo)
    queue_pages(transport, make_item, pages=2)
    result = await run_full_sweep(transport, repo, db, query_id=query_id, max_pages=2)

    await repo.delete_query(query_id)

    run = await repo.get_sweep_run(result.sweep_id)
    assert run is not None
    assert run.query_id is None
    assert len(await repo.sweep_candidates(result.sweep_id)) == len(result.candidates)


# --- The write path, reached through the browser --------------------------------------


class AlwaysMatches:
    """The photo check, stubbed to say yes, so the whole judged path actually runs."""

    async def judge(self, items: list[Any], target: Any) -> TriageBatch:
        return TriageBatch(
            results=[
                TriageItem(id=item.item_id, matches_target=True, confidence=0.9) for item in items
            ],
            usage=None,
        )


async def test_a_sweep_launched_from_the_browser_writes_only_to_the_sweep_tables(
    transport: ScriptedTransport,
    repo: Repo,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
    tmp_path: Any,
) -> None:
    """The same claim as the first test in this file, at S04's new entry point.

    A route is a second way in, and a second way in is a second chance to reach the wrong
    writer — through a request handler that has the whole `Repo` in scope, no less. The
    source-level guard above pins what `_run_magic_sweep()` may name; this pins what a real
    request actually wrote.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "app.db",
        web_enabled=True,
        web_auth_token=SecretStr("test-token-please-ignore"),
    )
    query_id = await seed_standing_watch(repo)
    queue_pages(transport, make_item, pages=2)

    sessions = SessionManager(db, transport)
    launched: list[Any] = []
    app = create_app(
        settings,
        repo,
        None,
        None,
        client=VintedClient(transport, sessions),
        sessions=sessions,
        triage=AlwaysMatches(),  # type: ignore[arg-type]
        verdict=None,
        launch=launched.append,
    )
    with TestClient(app) as test_client:
        test_client.cookies.set(SESSION_COOKIE, "test-token-please-ignore")
        response = test_client.post(
            "/api/magic-search/sweep",
            json={
                "params": PARAMS,
                "tld": "fr",
                "keywords": KEYWORDS,
                "visual_signature": "a boxy waterproof shell",
                "labels": {"brand": "Patagonia"},
                "max_pages": 2,
            },
        )
        assert response.status_code == 202
        await launched.pop()

    stored = await count(db, "sweep_candidates")
    assert stored > 0, "the empty counts below mean nothing unless the sweep stored something"
    assert await count(db, "items") == 0, "a web-launched sweep is still not the poller"
    assert await count(db, "outbox") == 0, "an outbox row is a Telegram alert"
    assert await count(db, "market") == 0
    # And the standing watch it ran beside is exactly where it was.
    assert (await repo.get_state(query_id)).newest_item_ts == PHOTO_TS
