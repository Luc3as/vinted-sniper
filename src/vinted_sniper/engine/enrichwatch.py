"""Noticing when the outside brain stops answering.

The enrichment loop is built to degrade quietly: a listing's webhook goes out, the chat
message waits a little for a verdict, and if none arrives the alert ships anyway. That is
the right behaviour — a silent agent must never cost an alert — but it means a dead agent
and a thoughtful one look identical from the chat. Twice now the loop has been down for
days before anyone noticed, because nothing was worse than usual: the alerts kept coming,
just plainer.

So the silence has to be counted somewhere. A single listing without a verdict says
nothing (the agent may have declined, timed out, or simply been slow). A run of them, all
past the window they were given, says the far end is not answering at all. That is the
only claim made here, and it is made once: a notice per outage, not per listing.
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

log = get_logger(__name__)

# Beyond the wait the listing was given, how much longer before its silence counts. A
# verdict that arrives late still arrives; only one that never comes is evidence.
GRACE_S = 600

# How many consecutive unanswered listings make an outage rather than a run of declines.
SILENT_STREAK = 5


@dataclass(frozen=True, slots=True)
class Silence:
    """What the far end has and has not answered lately."""

    unanswered_streak: int
    considered: int

    @property
    def is_outage(self) -> bool:
        return self.unanswered_streak >= SILENT_STREAK


class EnrichmentWatch:
    """Watches the thing that is allowed to say nothing."""

    def __init__(
        self,
        *,
        repo: Repo,
        settings: Settings,
        stop: asyncio.Event,
        announce: Callable[[str | Callable[[Translator], str]], Awaitable[None]] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._stop = stop
        self._announce = announce
        self._clock = clock or (lambda: int(time.time()))
        self._warned = False

    async def run(self, interval_s: float = 900.0) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.check()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval_s)

    async def check(self) -> Silence:
        """Count the run of listings the agent was asked about and never answered."""
        if not self._settings.enrichment_wait_s:
            # The loop is switched off; nothing is expected to answer.
            return Silence(unanswered_streak=0, considered=0)

        settled_before = self._clock() - self._settings.enrichment_wait_s - GRACE_S
        answers = await self._repo.enrichment_answers(
            settled_before=settled_before, limit=SILENT_STREAK * 2
        )

        streak = 0
        for answered in answers:
            if answered:
                break
            streak += 1
        silence = Silence(unanswered_streak=streak, considered=len(answers))

        await self._react(silence)
        return silence

    async def _react(self, silence: Silence) -> None:
        if not silence.is_outage:
            # One verdict back is enough to call it over: the far end is answering again.
            self._warned = False
            return
        if self._warned:
            return
        self._warned = True

        log.warning(
            "enrichment.silent",
            unanswered=silence.unanswered_streak,
            considered=silence.considered,
            waited_s=self._settings.enrichment_wait_s,
        )
        if self._announce is not None:
            await self._announce(
                lambda t: t(
                    "The AI check has gone quiet: the last {n} listings were sent for a "
                    "verdict and none came back. Alerts are still going out, just without "
                    "a score. Worth looking at the agent that answers them.",
                    n=silence.unanswered_streak,
                )
            )
