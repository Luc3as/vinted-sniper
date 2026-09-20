"""Noticing that the outside agent has stopped answering.

The whole point of these is that the failure they guard against is invisible: alerts keep
arriving, they are simply plainer. Nothing else in the app has a reason to complain, so
these tests are the only thing standing between a dead agent and another four-day outage.
"""

from __future__ import annotations

import asyncio
from typing import Any

from vinted_sniper import i18n
from vinted_sniper.config import Settings
from vinted_sniper.db.connection import Database
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine.enrichwatch import GRACE_S, EnrichmentWatch

NOW = 1_800_000_000
WAIT_S = 90


async def seed(
    db: Database,
    *,
    item_id: int,
    destination_id: int,
    sent_at: int,
    enriched: bool = False,
) -> None:
    """One listing that went out to a destination, with or without a verdict back."""
    await db.execute(
        "INSERT INTO items (item_id, query_id, tld, title, url, first_seen_at, enriched_at) "
        "VALUES (?, 1, 'sk', ?, ?, ?, ?)",
        (
            item_id,
            f"Item {item_id}",
            f"https://www.vinted.sk/items/{item_id}",
            sent_at,
            sent_at + 30 if enriched else None,
        ),
    )
    await db.execute(
        "INSERT INTO outbox (item_id, query_id, destination_id, status, kind, "
        "next_attempt_at, created_at, sent_at) VALUES (?, 1, ?, 'sent', 'new', ?, ?, ?)",
        (item_id, destination_id, sent_at, sent_at, sent_at),
    )


async def webhook_destination(repo: Repo) -> int:
    return await repo.add_destination(
        kind="webhook",
        name="agent",
        config={"url": "https://n8n.test/hook"},
    )


def make_watch(
    repo: Repo, settings: Settings, *, now: int = NOW
) -> tuple[EnrichmentWatch, list[str]]:
    announcements: list[str] = []

    async def announce(message: Any) -> None:
        announcements.append(message(i18n.get("en")) if callable(message) else message)

    watch = EnrichmentWatch(
        repo=repo,
        settings=settings.model_copy(update={"enrichment_wait_s": WAIT_S}),
        stop=asyncio.Event(),
        announce=announce,
        clock=lambda: now,
    )
    return watch, announcements


# The listing has been sent, and long enough ago that a verdict was due.
SETTLED = NOW - WAIT_S - GRACE_S - 60


async def test_a_run_of_listings_with_no_verdict_is_announced_once(
    repo: Repo, db: Database, settings: Settings
) -> None:
    destination = await webhook_destination(repo)
    for index in range(6):
        await seed(db, item_id=100 + index, destination_id=destination, sent_at=SETTLED - index)

    watch, announcements = make_watch(repo, settings)
    silence = await watch.check()

    assert silence.is_outage
    assert silence.unanswered_streak == 6
    assert len(announcements) == 1
    assert "last 6 listings" in announcements[0]
    assert "Alerts are still going out" in announcements[0]

    # A second pass while the outage continues must not nag.
    await watch.check()
    assert len(announcements) == 1


async def test_a_few_unanswered_listings_are_not_an_outage(
    repo: Repo, db: Database, settings: Settings
) -> None:
    """The agent is allowed to decline, time out, or be slow. Only a run is evidence."""
    destination = await webhook_destination(repo)
    for index in range(3):
        await seed(db, item_id=200 + index, destination_id=destination, sent_at=SETTLED - index)

    watch, announcements = make_watch(repo, settings)
    silence = await watch.check()

    assert not silence.is_outage
    assert silence.unanswered_streak == 3
    assert announcements == []


async def test_a_listing_still_inside_its_wait_window_is_not_counted_against_the_agent(
    repo: Repo, db: Database, settings: Settings
) -> None:
    """A verdict that has not arrived yet is not a verdict that never will."""
    destination = await webhook_destination(repo)
    for index in range(6):
        await seed(db, item_id=300 + index, destination_id=destination, sent_at=NOW - index)

    watch, announcements = make_watch(repo, settings)
    silence = await watch.check()

    assert silence.considered == 0
    assert not silence.is_outage
    assert announcements == []


async def test_one_verdict_coming_back_ends_the_streak_and_rearms_the_notice(
    repo: Repo, db: Database, settings: Settings
) -> None:
    destination = await webhook_destination(repo)
    for index in range(6):
        await seed(
            db, item_id=400 + index, destination_id=destination, sent_at=SETTLED - 100 - index
        )

    watch, announcements = make_watch(repo, settings)
    assert (await watch.check()).is_outage
    assert len(announcements) == 1

    # The agent answers again: the newest listing carries a verdict.
    await seed(db, item_id=499, destination_id=destination, sent_at=SETTLED, enriched=True)
    silence = await watch.check()
    assert silence.unanswered_streak == 0
    assert not silence.is_outage

    # And a fresh outage after that is worth saying out loud again.
    for index in range(6):
        await seed(
            db, item_id=500 + index, destination_id=destination, sent_at=SETTLED + 10 + index
        )
    assert (await watch.check()).is_outage
    assert len(announcements) == 2


async def test_chat_deliveries_are_not_mistaken_for_agent_deliveries(
    repo: Repo, db: Database, settings: Settings
) -> None:
    """Only webhook destinations are asked for a verdict; Telegram is never late."""
    telegram = await repo.add_destination(kind="telegram", name="chat", config={"chat_id": "1"})
    for index in range(6):
        await seed(db, item_id=600 + index, destination_id=telegram, sent_at=SETTLED - index)

    watch, announcements = make_watch(repo, settings)
    silence = await watch.check()

    assert silence.considered == 0
    assert announcements == []


async def test_the_loop_being_switched_off_is_never_reported_as_an_outage(
    repo: Repo, db: Database, settings: Settings
) -> None:
    destination = await webhook_destination(repo)
    for index in range(6):
        await seed(db, item_id=700 + index, destination_id=destination, sent_at=SETTLED - index)

    announcements: list[str] = []

    async def announce(message: Any) -> None:
        announcements.append(message(i18n.get("en")) if callable(message) else message)

    watch = EnrichmentWatch(
        repo=repo,
        settings=settings.model_copy(update={"enrichment_wait_s": 0}),
        stop=asyncio.Event(),
        announce=announce,
        clock=lambda: NOW,
    )
    silence = await watch.check()

    assert not silence.is_outage
    assert announcements == []
