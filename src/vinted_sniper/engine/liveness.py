"""Which recorded listings are still up.

Vinted's anonymous API never says "sold": a sold or withdrawn listing simply vanishes
from the seller's public wardrobe, and the two are indistinguishable from outside. So
liveness is a presence check — read the wardrobe, and anything recorded but no longer
present (or flagged closed) is gone. One request covers every recorded listing of that
seller, which is what makes the recheck affordable under the shared per-site budget.
"""

from __future__ import annotations

import asyncio
import contextlib

from vinted_sniper.db.repo import Repo
from vinted_sniper.log import get_logger
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.errors import VintedError

log = get_logger(__name__)

# A look every ~6 hours is enough: the badge answers "is this still worth clicking",
# not "when exactly did it sell". The hourly cycle with a seller cap spreads requests
# out instead of bursting every sixth hour.
CYCLE_INTERVAL_S = 3600.0
RECHECK_AFTER_S = 6 * 3600
SELLERS_PER_CYCLE = 25


class LivenessChecker:
    """Periodically marks recorded listings that vanished from their seller's wardrobe."""

    def __init__(
        self,
        repo: Repo,
        client: VintedClient,
        stop: asyncio.Event,
        *,
        interval_s: float = CYCLE_INTERVAL_S,
    ) -> None:
        self._repo = repo
        self._client = client
        self._stop = stop
        self._interval_s = interval_s

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_due()
            except Exception as exc:
                log.exception("liveness.cycle_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)

    async def check_due(self) -> None:
        """One cycle: look at every seller whose listings have gone unchecked too long."""
        due = await self._repo.sellers_due_liveness(
            recheck_after_s=RECHECK_AFTER_S, limit=SELLERS_PER_CYCLE
        )
        for tld, seller_id in due:
            if self._stop.is_set():
                return
            try:
                wardrobe = await self._client.seller_wardrobe(tld, seller_id)
            except VintedError as exc:
                # The site is limping; pressing on with more reads helps nobody.
                # Stop here — the next cycle picks these sellers up again.
                log.warning("liveness.read_failed", tld=tld, seller_id=seller_id, error=str(exc))
                return

            if wardrobe is None:
                # Deleted or hidden account: could not tell — which is NOT "everything
                # sold". Recording the look still pushes the next one ~6h out.
                await self._repo.record_liveness(tld, seller_id, ())
                continue

            recorded = await self._repo.live_item_ids(tld, seller_id)
            gone = {item_id for item_id in recorded if wardrobe.statuses.get(item_id)}
            if wardrobe.complete:
                # Only a full read makes absence meaningful; past the page cap an
                # unseen listing is merely unseen.
                gone |= recorded - wardrobe.statuses.keys()
            marked = await self._repo.record_liveness(tld, seller_id, gone)
            if marked:
                log.info("liveness.marked_gone", tld=tld, seller_id=seller_id, count=marked)
