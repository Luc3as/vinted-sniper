"""Whether a swept listing learns it has gone, and whether /magic says so.

A sweep candidate never gets an `items` row, so before 0017 the recheck could not see it
at all: /magic showed a listing that had been sold weeks ago exactly like a live one.
These tests pin that one wardrobe read now settles both tables, that a sweep's sellers
stop being due once the run is old, and that the result page prints the band.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.conftest import ScriptedTransport
from tests.integration.test_liveness import checker_for, queue_wardrobe, seed_item
from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo, SweepCandidate
from vinted_sniper.vinted.transport import Response
from vinted_sniper.web.server import SESSION_COOKIE, create_app


async def seed_sweep(
    repo: Repo,
    db: Any,
    *,
    item_id: int,
    seller_id: int | None = 7,
    tld: str = "sk",
    started_at: int | None = None,
) -> int:
    """One sweep holding one candidate, optionally backdated to age it out."""
    sweep_id = await repo.create_sweep_run(tld=tld, params={"catalog_ids": "1"}, keywords=[])
    if started_at is not None:
        await db.execute(
            "UPDATE sweep_runs SET started_at = ? WHERE id = ?", (started_at, sweep_id)
        )
    await repo.record_sweep_candidates(
        sweep_id,
        [
            SweepCandidate(
                item_id=item_id,
                title=f"Listing {item_id}",
                url=f"https://vinted.{tld}/items/{item_id}",
                photo_url=f"https://img.example/{item_id}.jpg",
                seller_id=seller_id,
            )
        ],
    )
    return sweep_id


async def candidate_row(db: Any, item_id: int) -> Any:
    rows = await db.fetch_all("SELECT * FROM sweep_candidates WHERE item_id = ?", (item_id,))
    return rows[0]


def web_client(repo: Repo, tmp_path: Path) -> tuple[TestClient, str]:
    token = "test-token-please-ignore"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "app.db",
        web_enabled=True,
        web_auth_token=SecretStr(token),
    )
    return TestClient(create_app(settings, repo)), token


# --- Who is due, once sweeps count too ----------------------------------------------


async def test_a_swept_listing_makes_its_seller_due(repo: Repo, db: Any) -> None:
    await seed_sweep(repo, db, item_id=1, seller_id=7)

    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == [("sk", 7)]


async def test_a_swept_listing_without_a_seller_id_is_never_due(repo: Repo, db: Any) -> None:
    # Rows written before 0017 have no seller id, and a wardrobe cannot be read without
    # one. They stay unchecked rather than being guessed at.
    await seed_sweep(repo, db, item_id=1, seller_id=None)

    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == []


async def test_an_old_sweeps_seller_ages_out_of_the_recheck(repo: Repo, db: Any) -> None:
    await seed_sweep(repo, db, item_id=1, started_at=int(time.time()) - 40 * 86_400)

    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == []


async def test_the_same_seller_is_offered_once_for_both_tables(repo: Repo, db: Any) -> None:
    # An alerted listing and a swept one from the same wardrobe must cost one read, not two.
    await seed_item(db, 1, seller_id=7)
    await seed_sweep(repo, db, item_id=2, seller_id=7)

    assert await repo.sellers_due_liveness(recheck_after_s=6 * 3600) == [("sk", 7)]


# --- What one read settles -----------------------------------------------------------


async def test_a_swept_listing_absent_from_the_wardrobe_is_marked_gone(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_sweep(repo, db, item_id=1)
    await seed_sweep(repo, db, item_id=2)
    queue_wardrobe(transport, [{"id": 1, "is_closed": False}])

    await checker_for(transport, repo, db).check_due()

    assert (await candidate_row(db, 1))["sold_at"] is None
    assert (await candidate_row(db, 1))["sold_checked_at"] is not None
    assert (await candidate_row(db, 2))["sold_at"] is not None


async def test_one_read_marks_the_alerted_and_the_swept_copy_together(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_item(db, 5, seller_id=7)
    await seed_sweep(repo, db, item_id=5, seller_id=7)
    queue_wardrobe(transport, [])

    await checker_for(transport, repo, db).check_due()

    assert (await db.fetch_all("SELECT sold_at FROM items"))[0]["sold_at"] is not None
    assert (await candidate_row(db, 5))["sold_at"] is not None


async def test_the_marked_count_is_listings_not_rows(repo: Repo, db: Any) -> None:
    # The same listing in `items` and in two sweeps is one listing gone, not three.
    await seed_item(db, 5, seller_id=7)
    await seed_sweep(repo, db, item_id=5, seller_id=7)
    await seed_sweep(repo, db, item_id=5, seller_id=7)

    assert await repo.record_liveness("sk", 7, [5]) == 1


async def test_an_unreadable_wardrobe_leaves_the_swept_copy_alone(
    transport: ScriptedTransport, repo: Repo, db: Any
) -> None:
    await seed_sweep(repo, db, item_id=1)
    transport.queue(Response(status_code=404, text="", headers={}, cookies={}))

    await checker_for(transport, repo, db).check_due()

    row = await candidate_row(db, 1)
    assert row["sold_at"] is None
    # The look still counts, so a hidden account is not hammered every cycle.
    assert row["sold_checked_at"] is not None


# --- The band on the pages -----------------------------------------------------------


async def test_the_sweep_page_bands_a_gone_listing_and_leaves_a_live_one(
    repo: Repo, db: Any, tmp_path: Path
) -> None:
    sweep_id = await seed_sweep(repo, db, item_id=1)
    await repo.record_sweep_candidates(
        sweep_id,
        [
            SweepCandidate(
                item_id=2,
                title="Listing 2",
                url="https://vinted.sk/items/2",
                photo_url="https://img.example/2.jpg",
                seller_id=7,
            )
        ],
    )
    await repo.record_liveness("sk", 7, [2])

    client, token = web_client(repo, tmp_path)
    with client as web:
        web.cookies.set(SESSION_COOKIE, token)
        page = web.get(f"/magic?sweep={sweep_id}").text

    assert page.count('class="sold-band"') == 1
    assert page.count("listing m-unchecked is-gone") == 1


async def test_found_bands_a_gone_listing_and_leaves_a_live_one(
    repo: Repo, db: Any, tmp_path: Path
) -> None:
    await seed_item(db, 1, seller_id=7)
    await seed_item(db, 2, seller_id=7, sold_at=int(time.time()))

    client, token = web_client(repo, tmp_path)
    with client as web:
        web.cookies.set(SESSION_COOKIE, token)
        page = web.get("/").text

    assert page.count('class="sold-band"') == 1
