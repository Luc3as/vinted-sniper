"""The Monday message: sent once per slot, survives restarts, says something useful."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from vinted_sniper import i18n
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine.report import WeeklyReport
from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.vinted.models import Item

ZONE = ZoneInfo("Europe/Bratislava")


def test_the_slot_is_the_most_recent_monday_morning() -> None:
    wed = datetime(2026, 9, 9, 15, 0, tzinfo=ZONE)
    assert WeeklyReport.latest_slot(wed) == datetime(2026, 9, 7, 8, 0, tzinfo=ZONE)
    early_monday = datetime(2026, 9, 7, 7, 59, tzinfo=ZONE)
    assert WeeklyReport.latest_slot(early_monday) == datetime(2026, 8, 31, 8, 0, tzinfo=ZONE)
    on_the_dot = datetime(2026, 9, 7, 8, 0, tzinfo=ZONE)
    assert WeeklyReport.latest_slot(on_the_dot) == on_the_dot


async def test_the_report_goes_out_once_per_slot(repo: Repo) -> None:
    sent: list[str] = []

    async def announce(message: Any) -> None:
        sent.append(message(i18n.get("en")) if callable(message) else message)

    clock = {"now": datetime(2026, 9, 7, 9, 0, tzinfo=ZONE)}
    report = WeeklyReport(
        repo=repo, zone=ZONE, stop=asyncio.Event(), announce=announce, now=lambda: clock["now"]
    )

    assert await report.tick() is True
    assert await report.tick() is False, "same slot, already sent"
    clock["now"] = datetime(2026, 9, 14, 8, 30, tzinfo=ZONE)
    assert await report.tick() is True
    assert len(sent) == 2


async def test_the_report_reads_like_a_summary(repo: Repo) -> None:
    query_id = await repo.add_query(
        name="Torrentshell",
        url="https://www.vinted.sk/catalog?search_text=torrentshell",
        tld="sk",
        params={"search_text": "torrentshell"},
        poll_interval_s=60,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    now = int(time.time())
    items = [
        Item(
            item_id=i,
            tld="sk",
            title=f"Bunda {i}",
            url=f"https://www.vinted.sk/items/{i}",
            price=Decimal(40),
            total_price=Decimal(45),
            currency="EUR",
            photo_ts=now,
        )
        for i in (1, 2, 3)
    ]
    await repo.record_new_items(query, items, [])
    await repo.store_enrichment(2, EnrichmentIn(score=91, verdict="Berte."))

    async def announce(_: Any) -> None:
        return None

    report = WeeklyReport(repo=repo, zone=ZONE, stop=asyncio.Event(), announce=announce)
    text = await report.compose(now - 3600)

    assert text.startswith("Weekly: 3 new listings across 1 search, 0 alerts sent, 0 price drops.")
    assert "1 judged by the agent (avg score 91)" in text
    assert "Best verdict: 91/100 — Bunda 2 at 45 EUR" in text
    assert "Busiest: Torrentshell (3)" in text

    slovak = await report.compose(now - 3600, i18n.get("sk"))
    assert slovak.startswith("Týždeň: 3 nové inzeráty v 1 hľadaní, 0 odoslaných alertov, 0 zliav.")
    assert "Najlepší verdikt: 91/100 — Bunda 2 za 45 EUR" in slovak
