"""A weekly word from the app: what turned up, what dropped, what the brain liked best.

Not a dashboard in a message — three or four lines you can read on a phone while the
coffee brews. It goes to destinations flagged for status notices, on Monday morning in
the configured timezone, and the last send is written down so a restart on Monday does
not repeat it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from vinted_sniper import i18n
from vinted_sniper.db.repo import Repo
from vinted_sniper.i18n import Translator
from vinted_sniper.log import get_logger

log = get_logger(__name__)

REPORT_WEEKDAY = 0  # Monday
REPORT_HOUR = 8
_LOOKBACK_S = 7 * 86_400
_CHECK_INTERVAL_S = 300.0


class WeeklyReport:
    def __init__(
        self,
        *,
        repo: Repo,
        zone: ZoneInfo,
        stop: asyncio.Event,
        announce: Callable[[str | Callable[[Translator], str]], Awaitable[None]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._zone = zone
        self._stop = stop
        self._announce = announce
        self._now = now or (lambda: datetime.now(self._zone))

    async def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=_CHECK_INTERVAL_S)

    async def tick(self) -> bool:
        """Send the report if its slot has come and it has not been sent yet. Returns
        whether one went out."""
        now = self._now()
        due = self.latest_slot(now)
        last = await self._repo.get_state_value("weekly_report_sent_at")
        if last and int(last) >= int(due.timestamp()):
            return False
        figures = await self._repo.weekly_figures(int(due.timestamp()) - _LOOKBACK_S)
        await self._announce(lambda t: self.render(figures, t))
        await self._repo.set_state_value("weekly_report_sent_at", str(int(due.timestamp())))
        log.info("report.sent", slot=due.isoformat())
        return True

    @staticmethod
    def latest_slot(now: datetime) -> datetime:
        """The most recent Monday 08:00 at or before `now`."""
        slot = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
        slot -= timedelta(days=(now.weekday() - REPORT_WEEKDAY) % 7)
        if slot > now:
            slot -= timedelta(days=7)
        return slot

    async def compose(self, since: int, t: Translator = i18n.EN) -> str:
        return self.render(await self._repo.weekly_figures(since), t)

    @staticmethod
    def render(f: dict[str, Any], t: Translator = i18n.EN) -> str:
        if f["found"] == 0 and f["drops"] == 0:
            return t(
                "Weekly: nothing new turned up on any search. Either the market is quiet "
                "or the searches are too narrow."
            )
        lines = [
            t(
                "Weekly: {found} across {searches}, {sent}, {drops}.",
                found=t.ngettext("{n} new listing", "{n} new listings", f["found"]),
                searches=t.ngettext("{n} search", "{n} searches", f["searches"]),
                sent=t.ngettext("{n} alert sent", "{n} alerts sent", f["sent"]),
                drops=t.ngettext("{n} price drop", "{n} price drops", f["drops"]),
            )
        ]
        if f["judged"]:
            avg = (
                t(" (avg score {score})", score=f"{f['avg_score']:.0f}")
                if f["avg_score"] is not None
                else ""
            )
            lines.append(
                t.ngettext(
                    "{n} judged by the agent{avg}.",
                    "{n} judged by the agent{avg}.",
                    f["judged"],
                    avg=avg,
                )
            )
        if f["hot"]:
            best = f["hot"][0]
            price = (
                t(
                    " at {price} {currency}",
                    price=f"{best['total_price']:.0f}",
                    currency=best["currency"] or "",
                ).rstrip()
                if best["total_price"] is not None
                else ""
            )
            lines.append(
                t(
                    "Best verdict: {score}/100 — {title}{price}.",
                    score=best["enrich_score"],
                    title=best["title"][:60],
                    price=price,
                )
            )
        if f["busiest"]:
            top = ", ".join(f"{row['name']} ({row['n']})" for row in f["busiest"])
            lines.append(t("Busiest: {list}.", list=top))
        if f["blocks_total"]:
            lines.append(t("Refusals from Vinted so far: {n}.", n=f["blocks_total"]))
        return "\n".join(lines)
