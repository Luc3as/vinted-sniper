"""The loop that watches one search.

Each search gets its own task. What matters here is what happens when a check fails: the
error types from the Vinted layer each mean something different, and treating them the same
is how a tool talks itself into a longer block. A rejected token is cheap to fix. A refused
connection is not, and hammering it makes it worse.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

from vinted_sniper.config import MAX_BACKOFF_S, Settings
from vinted_sniper.db.repo import Query, Repo
from vinted_sniper.engine import dedup, filters
from vinted_sniper.log import get_logger
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.errors import (
    AuthExpiredError,
    BlockedError,
    MalformedResponseError,
    NetworkError,
    RateLimitedError,
)
from vinted_sniper.vinted.models import Item
from vinted_sniper.vinted.session import SessionManager

log = get_logger(__name__)

# Spread checks out so a dozen searches do not all fire on the same second.
_JITTER_FRACTION = 0.15


class Poller:
    """Checks one saved search, forever, until asked to stop."""

    def __init__(
        self,
        query: Query,
        *,
        repo: Repo,
        client: VintedClient,
        sessions: SessionManager,
        settings: Settings,
        stop: asyncio.Event,
        work_available: asyncio.Event | None = None,
        rng: random.Random | None = None,
        initial_delay_s: float = 0.0,
        announce: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.query = query
        self._repo = repo
        self._client = client
        self._sessions = sessions
        self._settings = settings
        self._stop = stop
        self._work_available = work_available
        self._rng = rng or random.Random()
        self._initial_delay_s = initial_delay_s
        self._announce = announce
        self._consecutive_errors = 0
        self._log = log.bind(query_id=query.id, query=query.name, tld=query.tld)

    async def run(self) -> None:
        """Check on a schedule until the stop event is set."""
        if self._initial_delay_s > 0:
            await self._wait(self._initial_delay_s)
        while not self._stop.is_set():
            delay = await self.tick()
            await self._wait(delay)

    async def tick(self) -> float:
        """Run one check. Returns how long to wait before the next one."""
        # Someone else on this site was refused recently. Their backoff is ours too: the
        # address is what is being scored, and finding the block out for ourselves only
        # adds to it.
        holding = self._sessions.cooldown.wait_s(self.query.tld)
        if holding > 0:
            self._log.info("poll.cooling_down", retry_in_s=round(holding))
            return holding + self._rng.uniform(0, 5.0)

        try:
            await self._check()
        except AuthExpiredError as exc:
            # The anonymous token aged out. A new one costs a single page load.
            self._log.info("poll.session_expired", error=str(exc))
            await self._repo.record_failure(self.query.id, "http_401", str(exc))
            await self._sessions.invalidate(self.query.tld)
            return min(5.0, float(self.query.poll_interval_s))
        except BlockedError as exc:
            # Vinted is refusing this client. A fresh cookie will not change that, so drop
            # the session, back off hard, and hold every other search on this site with
            # us. The replacement session is fetched by the next check, not now — asking
            # again the same second is how a short challenge becomes a long block.
            self._consecutive_errors += 1
            await self._repo.record_failure(self.query.id, "http_403", str(exc))
            await self._sessions.discard_blocked(self.query.tld)
            delay = self._backoff()
            first_on_site = not self._sessions.cooldown.is_closed(self.query.tld)
            self._sessions.cooldown.close(self.query.tld, delay)
            self._log.warning("poll.blocked", error=str(exc), retry_in_s=round(delay))
            if first_on_site and self._announce is not None:
                with contextlib.suppress(Exception):
                    await self._announce(
                        f"vinted.{self.query.tld} is refusing requests from this address "
                        f"(\u201c{self.query.name}\u201d was the first to notice). Holding "
                        f"every search on that site for about {round(delay / 60)} min. "
                        "If this keeps happening, see the troubleshooting guide."
                    )
            return delay
        except RateLimitedError as exc:
            self._consecutive_errors += 1
            await self._repo.record_failure(self.query.id, "http_429", str(exc))
            delay = exc.retry_after or self._backoff()
            self._log.warning("poll.rate_limited", retry_in_s=round(delay))
            return float(delay)
        except MalformedResponseError as exc:
            # Not a retry problem: either the response shape changed or something was
            # served in its place. Keep going at normal pace and make it visible.
            self._consecutive_errors += 1
            await self._repo.record_failure(self.query.id, "malformed", str(exc))
            self._log.error("poll.malformed_response", error=str(exc))
            return self._interval()
        except NetworkError as exc:
            self._consecutive_errors += 1
            await self._repo.record_failure(self.query.id, "network", str(exc))
            delay = min(self._backoff(), 120.0)
            self._log.warning("poll.network_error", error=str(exc), retry_in_s=round(delay))
            return delay

        self._consecutive_errors = 0
        return self._interval()

    async def _check(self) -> None:
        items = await self._client.search(self.query.tld, self.query.params)
        state = await self._repo.get_state(self.query.id)

        candidates: list[Item] = []
        for item in items:
            if filters.check(item, self.query) is None:
                candidates.append(item)

        known = await self._repo.known_item_ids([item.item_id for item in candidates])
        if known and not state.is_first_run:
            await self._announce_price_drops([c for c in candidates if c.item_id in known])
        selection = dedup.select(
            candidates=candidates,
            all_items=items,
            known_ids=known,
            high_water_mark=state.newest_item_ts,
            now=int(time.time()),
            freshness_window_s=self._settings.freshness_window_min * 60,
            is_first_run=state.is_first_run,
            first_run_mode=self._settings.first_run_mode,
        )

        destination_ids = await self._repo.destination_ids_for_query(self.query.id)
        if selection.to_record:
            await self._repo.record_new_items(
                self.query,
                selection.to_record,
                destination_ids,
                notify=selection.to_notify,
                keep_raw=self._settings.keep_raw_json,
            )
            if selection.to_notify and self._work_available is not None:
                self._work_available.set()

        stale_cycles = state.stale_cycles
        if selection.newest_raw_ts is not None and selection.newest_raw_ts == state.newest_raw_ts:
            stale_cycles += 1
        else:
            stale_cycles = 0

        await self._repo.record_success(
            self.query.id,
            newest_raw_ts=selection.newest_raw_ts,
            newest_item_ts=selection.newest_item_ts,
            seen=len(items),
            stale_cycles=stale_cycles,
        )

        if state.is_first_run:
            self._log.info(
                "poll.seeded",
                recorded=len(selection.to_record),
                announced=len(selection.to_notify),
                mode=self._settings.first_run_mode,
            )
        elif selection.to_notify:
            self._log.info("poll.new_items", count=len(selection.to_notify), returned=len(items))
        else:
            self._log.debug("poll.nothing_new", returned=len(items))

    async def _announce_price_drops(self, seen_again: list[Item]) -> None:
        """A listing already recorded, now cheaper. No extra request: it is on the page
        we just fetched, and the page carries its current price."""
        threshold = self._settings.price_drop_min_percent
        if threshold <= 0 or not seen_again:
            return
        previous = await self._repo.current_prices([item.item_id for item in seen_again])
        drops: list[tuple[Item, Decimal]] = []
        for item in seen_again:
            before = previous.get(item.item_id)
            now_payable = item.total_price if item.total_price is not None else item.price
            if before is None or now_payable is None or before <= 0:
                continue
            fall = (before - now_payable) / before * 100
            if fall >= threshold:
                drops.append((item, before))
        if not drops:
            return
        destination_ids = await self._repo.destination_ids_for_query(self.query.id)
        await self._repo.record_price_drops(self.query, drops, destination_ids)
        if destination_ids and self._work_available is not None:
            self._work_available.set()
        self._log.info(
            "poll.price_drops",
            count=len(drops),
            items=[(item.item_id, str(before), str(item.total_price)) for item, before in drops],
        )

    def _interval(self) -> float:
        base = float(self.query.poll_interval_s)
        return base + self._rng.uniform(0, base * _JITTER_FRACTION)

    def _backoff(self) -> float:
        """Exponential backoff with jitter, capped so a search never sleeps forever."""
        step = min(2.0**self._consecutive_errors * self.query.poll_interval_s, MAX_BACKOFF_S)
        return step + self._rng.uniform(0, step * _JITTER_FRACTION)

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately if the app is shutting down."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
