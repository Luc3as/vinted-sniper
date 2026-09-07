"""One address, many searches: the parts that keep them from behaving like a botnet."""

from __future__ import annotations

import asyncio
from typing import Any

from tests.conftest import ScriptedTransport
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.pacing import RequestBudget, SiteCooldown
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.transport import TransportPool


async def test_searches_starting_together_share_one_handshake(
    transport: ScriptedTransport, db: Any
) -> None:
    """Seven searches on an empty cache used to mean seven homepage loads inside a second,
    each with its own randomly chosen browser persona. One address, seven browsers, one
    second: the anti-bot system does not need a second clue."""
    sessions = SessionManager(db, transport)

    results = await asyncio.gather(*(sessions.get("fr") for _ in range(7)))

    homepage_loads = [r for r in transport.requests if "/api/v2/" not in r["url"]]
    assert len(homepage_loads) == 1
    assert len({id(session) for session in results}) == 1, "everyone gets the same session"
    assert len({session.identity.user_agent for session in results}) == 1


async def test_a_replacement_session_starts_on_a_fresh_client(db: Any) -> None:
    """A "new" session on the old client is not new at all: the client keeps its own
    cookie jar and connections, and the anti-bot system links the fresh cookie straight
    back to the visitor it already flagged. Every bootstrap must rebuild the client."""
    transports: list[ScriptedTransport] = []

    def build(proxy: str | None) -> ScriptedTransport:
        built = ScriptedTransport()
        transports.append(built)
        return built

    sessions = SessionManager(db, TransportPool(build))

    await sessions.get("fr")
    await sessions.rotate("fr")

    assert len(transports) == 2, "the second session rode the first session's client"
    assert transports[0].closed


async def test_sites_are_bootstrapped_independently(transport: ScriptedTransport, db: Any) -> None:
    sessions = SessionManager(db, transport)

    await asyncio.gather(sessions.get("fr"), sessions.get("de"))

    homepage_loads = [r for r in transport.requests if "/api/v2/" not in r["url"]]
    assert len(homepage_loads) == 2


async def test_the_budget_paces_catalog_requests_and_handshakes_alike(
    transport: ScriptedTransport, db: Any
) -> None:
    clock = _Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    budget = RequestBudget(per_minute=6, burst=1, clock=clock, sleep=sleep)
    sessions = SessionManager(db, transport, budget=budget)
    client = VintedClient(transport, sessions, budget=budget)

    for _ in range(3):
        transport.queue_catalog([])
        await client.search("fr", {"search_text": "x"})

    # Handshake + three catalog calls = four requests; the first is free (burst of one),
    # each of the rest waits out a ten-second slot at six per minute.
    assert len(transport.requests) == 4
    assert len(slept) == 3
    assert all(abs(s - 10.0) < 0.01 for s in slept)


async def test_budgets_are_per_site() -> None:
    clock = _Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds

    budget = RequestBudget(per_minute=6, burst=1, clock=clock, sleep=sleep)
    await budget.acquire("fr")
    await budget.acquire("de")

    assert slept == [], "a request to one site should not be charged against another"


def test_the_cooldown_is_per_site_and_only_ever_extends() -> None:
    clock = _Clock()
    cooldown = SiteCooldown(clock=clock)

    assert not cooldown.is_closed("fr")
    cooldown.close("fr", 300)
    assert cooldown.is_closed("fr")
    assert not cooldown.is_closed("de")

    cooldown.close("fr", 60)
    assert cooldown.wait_s("fr") == 300, "a shorter hold must not cut an existing one short"

    clock.now += 301
    assert not cooldown.is_closed("fr")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
