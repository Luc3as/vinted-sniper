"""The watchdog, and the storage guarantees everything else assumes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from tests.conftest import ScriptedTransport
from vinted_sniper import i18n
from vinted_sniper.config import Settings
from vinted_sniper.db.connection import Database
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import health
from vinted_sniper.engine.watchdog import Watchdog
from vinted_sniper.vinted.models import Item
from vinted_sniper.vinted.session import SessionManager


async def add_search(repo: Repo, name: str, tld: str = "fr") -> int:
    return await repo.add_query(
        name=name,
        url=f"https://www.vinted.{tld}/catalog?search_text={name}",
        tld=tld,
        params={"search_text": name},
        poll_interval_s=60,
    )


async def set_state(db: Database, query_id: int, **columns: Any) -> None:
    assignments = ", ".join(f"{key} = ?" for key in columns)
    await db.execute(
        f"UPDATE query_state SET {assignments} WHERE query_id = ?",
        (*columns.values(), query_id),
    )


def make_watchdog(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> tuple[Watchdog, list[str]]:
    announcements: list[str] = []

    async def announce(message: Any) -> None:
        announcements.append(message(i18n.get("en")) if callable(message) else message)

    watchdog = Watchdog(
        repo=repo,
        sessions=SessionManager(db, transport),
        settings=settings,
        stop=asyncio.Event(),
        announce=announce,
    )
    return watchdog, announcements


async def test_a_frozen_search_is_spotted_when_its_neighbours_keep_moving(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    stuck = await add_search(repo, "stuck")
    healthy = await add_search(repo, "healthy")
    now = int(time.time())

    await set_state(db, stuck, last_success_at=now, stale_cycles=15, newest_raw_ts=now - 9000)
    await set_state(db, healthy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 30)

    watchdog, announcements = make_watchdog(repo, db, transport, settings)
    stale = await watchdog.check()

    assert [s.query_id for s in stale] == [stuck]
    assert announcements, "the point is that you find out"


async def test_a_genuinely_quiet_site_is_not_mistaken_for_a_frozen_one(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    first = await add_search(repo, "one")
    second = await add_search(repo, "two")
    now = int(time.time())

    # Nothing new anywhere on the site: a slow night, not a stuck feed.
    await set_state(db, first, last_success_at=now, stale_cycles=20, newest_raw_ts=now - 9000)
    await set_state(db, second, last_success_at=now, stale_cycles=20, newest_raw_ts=now - 9000)

    watchdog, announcements = make_watchdog(repo, db, transport, settings)

    assert await watchdog.check() == []
    assert announcements == []


async def test_a_search_that_has_never_run_is_not_called_stale(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    new = await add_search(repo, "new")
    busy = await add_search(repo, "busy")
    now = int(time.time())
    await set_state(db, busy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 10)
    await set_state(db, new, stale_cycles=99)

    watchdog, _ = make_watchdog(repo, db, transport, settings)

    assert await watchdog.check() == []


async def test_the_same_search_is_only_reported_once(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    stuck = await add_search(repo, "stuck")
    healthy = await add_search(repo, "healthy")
    now = int(time.time())
    await set_state(db, stuck, last_success_at=now, stale_cycles=15, newest_raw_ts=now - 9000)
    await set_state(db, healthy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 30)

    watchdog, announcements = make_watchdog(repo, db, transport, settings)
    await watchdog.check()
    await watchdog.check()
    await watchdog.check()

    assert len(announcements) == 1, "a stuck search should not become a stuck notification"


async def test_being_stale_starts_a_new_session(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    stuck = await add_search(repo, "stuck")
    healthy = await add_search(repo, "healthy")
    now = int(time.time())
    await set_state(db, stuck, last_success_at=now, stale_cycles=15, newest_raw_ts=now - 9000)
    await set_state(db, healthy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 30)

    watchdog, _ = make_watchdog(repo, db, transport, settings)
    await watchdog.check()

    assert any("/api/v2/" not in request["url"] for request in transport.requests)


# --- Health reporting ---------------------------------------------------------------


async def test_the_health_view_separates_quiet_from_broken(repo: Repo, db: Database) -> None:
    ok = await add_search(repo, "fine")
    broken = await add_search(repo, "broken")
    now = int(time.time())

    await set_state(db, ok, last_success_at=now, last_status="ok")
    await set_state(db, broken, last_success_at=now - 600, last_status="http_403", count_403=4)

    snapshot = await health.snapshot(repo)
    states = {search.name: search.state for search in snapshot.searches}

    assert states == {"fine": "ok", "broken": "failing"}


async def test_a_missing_heartbeat_reads_as_not_running(repo: Repo) -> None:
    assert await health.is_alive(repo) is False

    await repo.heartbeat()

    assert await health.is_alive(repo) is True


# --- Storage ------------------------------------------------------------------------


async def test_concurrent_writers_do_not_trip_over_each_other(repo: Repo, db: Database) -> None:
    """SQLite allows one writer at a time; writes are serialised so that is never an error."""
    query_id = await add_search(repo, "busy")

    async def bump(index: int) -> None:
        await db.execute(
            "UPDATE query_state SET items_seen_total = items_seen_total + 1 WHERE query_id = ?",
            (query_id,),
        )
        await repo.set_state_value(f"key-{index}", str(index))

    await asyncio.gather(*(bump(index) for index in range(25)))

    state = await repo.get_state(query_id)
    assert state.items_seen_total == 25, "no update may be lost to a lock"


async def test_reads_work_while_a_write_transaction_is_open(repo: Repo, db: Database) -> None:
    query_id = await add_search(repo, "one")

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE query_state SET items_seen_total = 7 WHERE query_id = ?", (query_id,)
        )
        # Reads do not queue behind the write lock, which is what keeps the dashboard
        # responsive while searches are being recorded.
        rows = await db.fetch_all("SELECT * FROM queries")
        assert len(rows) == 1

    assert (await repo.get_state(query_id)).items_seen_total == 7


async def test_a_failed_transaction_leaves_nothing_behind(repo: Repo, db: Database) -> None:
    query_id = await add_search(repo, "one")

    try:
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE query_state SET items_seen_total = 99 WHERE query_id = ?", (query_id,)
            )
            raise RuntimeError("something went wrong mid-write")
    except RuntimeError:
        pass

    assert (await repo.get_state(query_id)).items_seen_total == 0


async def test_deleting_a_search_takes_its_state_and_routes_with_it(
    repo: Repo, db: Database
) -> None:
    query_id = await add_search(repo, "one")
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.route(query_id, destination_id)

    await repo.delete_query(query_id)

    assert await db.fetch_one("SELECT 1 FROM query_state WHERE query_id = ?", (query_id,)) is None
    assert await repo.destination_ids_for_query(query_id) == []
    assert await repo.get_destination(destination_id) is not None, "the destination survives"


async def test_old_listings_are_pruned(repo: Repo, db: Database) -> None:
    query_id = await add_search(repo, "one")
    query = await repo.get_query(query_id)
    assert query is not None

    await repo.record_new_items(
        query,
        [Item(item_id=1, tld="fr", title="old", url="https://www.vinted.fr/items/1")],
        [],
    )
    await db.execute("UPDATE items SET first_seen_at = ?", (int(time.time()) - 90 * 86_400,))

    assert await repo.prune_items(older_than_days=30) == 1
    assert await repo.known_item_ids([1]) == set()


async def test_a_niche_search_is_not_stale_just_because_a_busy_one_keeps_moving(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    """'Rab Downpour' sees a listing a week; 'waterproof jacket' one every few minutes.
    Measured against the busy one, the niche one looked frozen forever."""
    niche = await add_search(repo, "rab downpour")
    busy = await add_search(repo, "waterproof jacket")
    now = int(time.time())

    # The niche search's page is not even full.
    await set_state(
        db, niche, last_success_at=now, stale_cycles=12, newest_raw_ts=now - 86_400, last_returned=4
    )
    await set_state(db, busy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 30)

    watchdog, announcements = make_watchdog(repo, db, transport, settings)
    assert await watchdog.check() == []
    assert announcements == []


async def test_a_full_page_search_is_judged_against_its_own_pace(
    repo: Repo, db: Database, transport: ScriptedTransport, settings: Settings
) -> None:
    slow = await add_search(repo, "slow but full")
    busy = await add_search(repo, "busy")
    now = int(time.time())
    query = await repo.get_query(slow)
    assert query is not None

    # This search normally sees one listing every ~4 hours.
    listings = [
        Item(
            item_id=i,
            tld="fr",
            title=f"x{i}",
            url=f"https://www.vinted.fr/items/{i}",
            photo_ts=now - i * 4 * 3600,
        )
        for i in range(1, 13)
    ]
    await repo.record_new_items(query, listings, [])

    await set_state(db, busy, last_success_at=now, stale_cycles=0, newest_raw_ts=now - 30)
    watchdog, _ = make_watchdog(repo, db, transport, settings)

    # Quiet for 10 hours with a 4-hour rhythm: unremarkable.
    await set_state(
        db,
        slow,
        last_success_at=now,
        stale_cycles=12,
        newest_raw_ts=now - 10 * 3600,
        last_returned=96,
    )
    assert await watchdog.check() == []

    # Quiet for 30 hours — more than six times its usual gap — with a full page: stuck.
    await set_state(db, slow, newest_raw_ts=now - 30 * 3600)
    assert [s.query_id for s in await watchdog.check()] == [slow]
