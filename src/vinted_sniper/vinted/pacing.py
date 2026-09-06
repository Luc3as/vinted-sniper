"""Keeping every search on one site inside a single, shared pace.

Anti-bot systems score an *address*, not a search. Ten searches each politely checking every
ten minutes still add up to one address making a request every minute, and when one of them
gets refused the other nine carrying on at full speed is exactly what turns a short challenge
into a long block. So two things live here that the pollers share rather than own:

* A request budget per country site, so the total rate from this address stays bounded no
  matter how many searches are configured. Homepage loads count too — they are the most
  scrutinised request of all.
* A cooldown per country site. The first search to be refused closes it for everyone on that
  site; the rest wait it out instead of each discovering the block for themselves.

Both take an injectable clock so the tests can prove the timing without living through it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from vinted_sniper.ratelimit import Gate, TokenBucket


class RequestBudget:
    """One token bucket per country site, created on first use."""

    def __init__(
        self,
        per_minute: float,
        *,
        burst: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self._rate_per_s = per_minute / 60.0
        self._burst = burst
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[str, TokenBucket] = {}

    async def acquire(self, tld: str) -> float:
        """Wait for permission to make one request to a site. Returns how long it waited."""
        bucket = self._buckets.get(tld)
        if bucket is None:
            bucket = TokenBucket(
                self._rate_per_s, capacity=self._burst, clock=self._clock, sleep=self._sleep
            )
            self._buckets[tld] = bucket
        return await bucket.acquire()


class SiteCooldown:
    """A site-wide hold, closed by whichever search was refused first."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._gates: dict[str, Gate] = {}

    def close(self, tld: str, seconds: float) -> None:
        """Hold every search on this site for at least `seconds` more."""
        self._gates.setdefault(tld, Gate(clock=self._clock)).close_for(seconds)

    def wait_s(self, tld: str) -> float:
        """Seconds until the site may be asked again; zero when it may be asked now."""
        gate = self._gates.get(tld)
        return gate.wait_s if gate is not None else 0.0

    def is_closed(self, tld: str) -> bool:
        return self.wait_s(tld) > 0

    def clear(self, tld: str) -> None:
        self._gates.pop(tld, None)
