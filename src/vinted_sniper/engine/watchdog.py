"""Noticing when Vinted stops telling us about new listings.

There is a failure mode that looks exactly like success: after a while, the catalog keeps
answering with a cheerful 200 and the same results it gave an hour ago, while new listings
pile up behind it. Nothing errors, nothing retries, and the alerts simply stop. People
assume the tool is broken and cannot tell why, because from the inside everything is fine.

The tell is comparative. A search whose newest listing has not moved for many checks might
just be a quiet corner of the market — but if a different search on the same site is still
turning up new things, the quiet one is stuck rather than slow. That distinction is the
whole trick, and it is why this runs across searches instead of inside the poller.

Comparing across searches has a blind spot, though: velocities differ wildly. "Waterproof
jacket" sees a new listing every few minutes; "Rab Downpour" a few a week. Measured against
the busy one, the niche one is "stale" forever. So a search is also judged against its own
history — how often it normally sees a listing — and a search whose page is not even full
is never called stale: a frozen feed serves the same full page over and over, a niche
search serves what little there is.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.i18n import Translator
from vinted_sniper.log import get_logger
from vinted_sniper.vinted.client import PER_PAGE
from vinted_sniper.vinted.errors import BlockedError, NetworkError
from vinted_sniper.vinted.session import SessionManager

log = get_logger(__name__)

# How long a site must have been advancing for us to trust it as a comparison.
_RECENT_MOVEMENT_S = 900

# A search is only suspicious once it has been quiet for this many times its own usual gap
# between listings, and for at least this long in absolute terms.
_QUIET_MULTIPLE = 6
_QUIET_FLOOR_S = 2 * 3600


@dataclass(frozen=True, slots=True)
class StaleSearch:
    query_id: int
    name: str
    tld: str
    stale_cycles: int
    last_new_listing_at: int | None


class Watchdog:
    """Watches the watchers."""

    def __init__(
        self,
        *,
        repo: Repo,
        sessions: SessionManager,
        settings: Settings,
        stop: asyncio.Event,
        announce: Callable[[str | Callable[[Translator], str]], Awaitable[None]] | None = None,
    ) -> None:
        self._repo = repo
        self._sessions = sessions
        self._settings = settings
        self._stop = stop
        self._announce = announce
        self._already_warned: set[int] = set()

    async def run(self, interval_s: float = 120.0) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval_s)

    async def check(self) -> list[StaleSearch]:
        """Find searches that have gone quiet while their neighbours have not."""
        queries = {query.id: query for query in await self._repo.list_queries(include_paused=False)}
        if not queries:
            return []

        states = {state.query_id: state for state in await self._repo.all_states()}
        now = int(time.time())

        moving_sites = {
            queries[query_id].tld
            for query_id, state in states.items()
            if query_id in queries
            and state.last_success_at is not None
            and state.stale_cycles == 0
            and now - state.last_success_at < _RECENT_MOVEMENT_S
        }

        stale: list[StaleSearch] = []
        for query_id, state in states.items():
            query = queries.get(query_id)
            if query is None or state.is_first_run:
                continue
            if state.stale_cycles < self._settings.watchdog_stale_cycles:
                self._already_warned.discard(query_id)
                continue
            if query.tld not in moving_sites:
                # The whole site is quiet. That is a slow night, not a stuck feed.
                continue
            if state.last_returned is not None and state.last_returned < PER_PAGE:
                # Not even a full page: a niche search, not a frozen one.
                self._already_warned.discard(query_id)
                continue
            if not await self._quiet_for_its_own_standards(query_id, state.newest_raw_ts, now):
                self._already_warned.discard(query_id)
                continue
            stale.append(
                StaleSearch(
                    query_id=query_id,
                    name=query.name,
                    tld=query.tld,
                    stale_cycles=state.stale_cycles,
                    last_new_listing_at=state.newest_raw_ts,
                )
            )

        for search in stale:
            await self._react(search)
        return stale

    async def _quiet_for_its_own_standards(
        self, query_id: int, newest_raw_ts: int | None, now: int
    ) -> bool:
        """Has this search been silent far longer than it usually is between listings?"""
        if newest_raw_ts is None:
            return True
        quiet_for = now - newest_raw_ts
        gap = await self._repo.typical_listing_gap_s(query_id)
        if gap is None:
            return quiet_for >= _QUIET_FLOOR_S
        return quiet_for >= max(_QUIET_FLOOR_S, gap * _QUIET_MULTIPLE)

    async def _react(self, search: StaleSearch) -> None:
        if search.query_id in self._already_warned:
            return
        self._already_warned.add(search.query_id)

        log.warning(
            "watchdog.stale_search",
            query_id=search.query_id,
            query=search.name,
            tld=search.tld,
            checks_without_new_listings=search.stale_cycles,
            action=self._settings.watchdog_action,
        )

        if self._settings.watchdog_action == "rotate":
            if self._sessions.cooldown.is_closed(search.tld):
                # The site is already holding us off; a fresh handshake now would only
                # add to the score that caused it.
                log.info("watchdog.rotation_skipped", tld=search.tld, reason="site cooling down")
            else:
                try:
                    await self._sessions.rotate(search.tld)
                    log.info("watchdog.session_rotated", tld=search.tld)
                except (BlockedError, NetworkError) as exc:
                    log.warning("watchdog.rotation_failed", tld=search.tld, error=str(exc))

        if self._announce is not None:
            await self._announce(
                lambda t: t(
                    "“{name}” has seen nothing new for {cycles} checks while other vinted.{tld} "
                    "searches keep finding listings. Started a fresh session; if it stays quiet, "
                    "see the troubleshooting guide.",
                    name=search.name,
                    cycles=search.stale_cycles,
                    tld=search.tld,
                )
            )
