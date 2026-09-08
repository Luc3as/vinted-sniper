"""Whether the sold-status recheck tells "gone" from "unseen" and "could not tell".

The anonymous API never says "sold" — listings just vanish from the seller's public
wardrobe. These tests pin the three answers a wardrobe read can give (absent, present
but closed, unreadable) and that only a complete read may turn absence into "gone".
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.conftest import ScriptedTransport
from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine.liveness import LivenessChecker
from vinted_sniper.vinted.client import WARDROBE_PAGE_CAP, VintedClient
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.transport import Response
from vinted_sniper.web.server import SESSION_COOKIE, create_app


async def seed_item(
    db: Any,
    item_id: int,
    *,
    seller_id: int | None = 7,
    tld: str = "sk",
    sold_at: int | None = None,
    sold_checked_at: int | None = None,
) -> None:
    await db.execute(
        "INSERT INTO items (item_id, tld, url, first_seen_at, seller_id, sold_at, "
        "sold_checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            tld,
            f"https://vinted.{tld}/items/{item_id}",
            int(time.time()),
            seller_id,
            sold_at,
            sold_checked_at,
        ),
    )


def queue_wardrobe(
    transport: ScriptedTransport,
    entries: list[dict[str, Any]],
    *,
    total_pages: int = 1,
    total_entries: int | None = None,
) -> None:
    transport.queue(
        Response(
            status_code=200,
            text=json.dumps(
                {
                    "items": entries,
                    "pagination": {
                        "total_pages": total_pages,
                        "total_entries": total_entries
                        if total_entries is not None
                        else len(entries),
                    },
                }
            ),
            headers={"content-type": "application/json"},
            cookies={},
        )
    )


def checker_for(transport: ScriptedTransport, repo: Repo, db: Any) -> LivenessChecker:
    sessions = SessionManager(db, transport)
    client = VintedClient(transport, sessions)
    return LivenessChecker(repo, client, asyncio.Event())


# --- Repo: who is due, and what a read changes --------------------------------------


async def test_an_unchecked_seller_is_due_and_a_fresh_one_is_not(repo: Repo, db: Any) -> None:
    await seed_item(db, 1, seller_id=7)
    await seed_item(db, 2, seller_id=8, sold_checked_at=int(time.time()))

    due = await repo.sellers_due_liveness(recheck_after_s=6 * 3600)

    assert due == [("sk", 7)]


async def test_a_seller_with_only_sold_items_is_never_rechecked(repo: Repo, db: Any) -> None:
    await seed_item(db, 1, seller_id=7, sold_at=int(time.time()) - 100)

    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == []


async def test_recording_a_read_stamps_the_live_and_buries_the_gone(repo: Repo, db: Any) -> None:
    await seed_item(db, 1, seller_id=7)
    await seed_item(db, 2, seller_id=7)

    marked = await repo.record_liveness("sk", 7, [2])

    assert marked == 1
    rows = {r["item_id"]: r for r in await db.fetch_all("SELECT * FROM items")}
    assert rows[1]["sold_at"] is None
    assert rows[1]["sold_checked_at"] is not None
    assert rows[2]["sold_at"] is not None
    # The seller is no longer due: the one live listing was just checked.
    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == []


# --- Checker: the three answers a wardrobe can give ---------------------------------


async def test_a_listing_absent_from_a_complete_wardrobe_is_gone(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_item(db, 1, seller_id=7)
    await seed_item(db, 2, seller_id=7)
    queue_wardrobe(transport, [{"id": 1, "is_closed": False}])

    await checker_for(transport, repo, db).check_due()

    rows = {r["item_id"]: r for r in await db.fetch_all("SELECT * FROM items")}
    assert rows[1]["sold_at"] is None
    assert rows[2]["sold_at"] is not None


async def test_a_listing_present_but_closed_is_gone_too(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_item(db, 1, seller_id=7)
    queue_wardrobe(transport, [{"id": 1, "is_closed": True}])

    await checker_for(transport, repo, db).check_due()

    row = (await db.fetch_all("SELECT * FROM items"))[0]
    assert row["sold_at"] is not None


async def test_an_incomplete_wardrobe_never_turns_absence_into_sold(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_item(db, 1, seller_id=7)
    # Every page claims more pages exist than the cap allows reading, so the read
    # ends incomplete and absence proves nothing.
    for _ in range(WARDROBE_PAGE_CAP):
        queue_wardrobe(
            transport, [{"id": 99, "is_closed": False}], total_pages=WARDROBE_PAGE_CAP + 1
        )

    await checker_for(transport, repo, db).check_due()

    row = (await db.fetch_all("SELECT * FROM items"))[0]
    assert row["sold_at"] is None
    assert row["sold_checked_at"] is not None


async def test_an_unreadable_wardrobe_means_could_not_tell_not_all_sold(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_item(db, 1, seller_id=7)
    transport.queue(Response(status_code=404, text="", headers={}, cookies={}))

    await checker_for(transport, repo, db).check_due()

    row = (await db.fetch_all("SELECT * FROM items"))[0]
    assert row["sold_at"] is None
    # The look still counts, so a hidden account is not hammered every cycle.
    assert row["sold_checked_at"] is not None


# --- The badge on /found -------------------------------------------------------------


async def test_a_gone_listing_wears_the_badge_and_a_live_one_does_not(
    repo: Repo, db: Any, tmp_path: Path
) -> None:
    await seed_item(db, 1, seller_id=7)
    await seed_item(db, 2, seller_id=7, sold_at=int(time.time()))

    token = "test-token-please-ignore"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "app.db",
        web_enabled=True,
        web_auth_token=SecretStr(token),
    )
    with TestClient(create_app(settings, repo)) as web:
        web.cookies.set(SESSION_COOKIE, token)
        page = web.get("/").text

    assert page.count("tag gone") == 1
