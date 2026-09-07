"""How the poller behaves when things go wrong.

These are the cases that decide whether the app survives a week unattended, so they run
against the real poller, session manager and database rather than stand-ins.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import ScriptedTransport
from vinted_sniper import i18n
from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import health
from vinted_sniper.engine.poller import Poller
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.pacing import SiteCooldown
from vinted_sniper.vinted.proxies import ProxyRotation
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.transport import Response, TransportPool


async def make_poller(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    *,
    db: Any,
    max_total_price: str | None = None,
    clock: FakeClock | None = None,
    announce: Any = None,
) -> tuple[Poller, asyncio.Event]:
    query_id = await repo.add_query(
        name="test search",
        url="https://www.vinted.fr/catalog?search_text=nike",
        tld="fr",
        params={"search_text": "nike", "order": "newest_first"},
        poll_interval_s=60,
        max_total_price=Decimal(max_total_price) if max_total_price else None,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    return poller_for(query, transport, repo, settings, db=db, clock=clock, announce=announce)


class FakeClock:
    """A monotonic clock the test moves by hand, for the site-wide cooldown."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def poller_for(
    query: Any,
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    *,
    db: Any,
    clock: FakeClock | None = None,
    announce: Any = None,
) -> tuple[Poller, asyncio.Event]:
    """Build a poller over an existing search, as a restarted process would."""
    cooldown = SiteCooldown(clock=clock) if clock is not None else None
    sessions = SessionManager(db, transport, cooldown=cooldown)
    client = VintedClient(transport, sessions)
    work = asyncio.Event()
    poller = Poller(
        query,
        repo=repo,
        client=client,
        sessions=sessions,
        settings=settings,
        stop=asyncio.Event(),
        work_available=work,
        announce=announce,
    )
    return poller, work


async def test_empty_results_are_a_success_not_a_failure(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    poller, work = await make_poller(transport, repo, settings, db=db)
    transport.queue_catalog([])

    delay = await poller.tick()

    state = await repo.get_state(poller.query.id)
    assert state.last_status == "ok"
    assert state.last_success_at is not None
    assert await repo.outbox_depth() == 0
    assert not work.is_set()
    assert delay >= settings.poll_default_interval_s


async def test_first_check_records_without_announcing(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, work = await make_poller(transport, repo, settings, db=db)
    destination_id = await repo.add_destination(kind="webhook", name="test", config={"url": "x"})
    await repo.route(poller.query.id, destination_id)

    now = int(time.time())
    transport.queue_catalog([make_item(i, photo_ts=now - i) for i in range(1, 6)])

    await poller.tick()

    assert await repo.outbox_depth() == 0, "a new search must not replay the catalog"
    assert len(await repo.known_item_ids([1, 2, 3, 4, 5])) == 5, "but it must remember them"
    assert not work.is_set()


async def test_first_check_in_newest_mode_announces_exactly_one(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """A regression guard.

    The whole page has to be recorded so none of it is ever mistaken for new, but only one
    listing may be announced. Queueing a notification per recorded listing is exactly the
    opening flood this mode exists to avoid, and it is an easy mistake to make because the
    two lists differ only here.
    """
    poller, _ = await make_poller(
        transport, repo, settings.model_copy(update={"first_run_mode": "newest"}), db=db
    )
    destination_id = await repo.add_destination(kind="webhook", name="test", config={"url": "x"})
    await repo.route(poller.query.id, destination_id)

    now = int(time.time())
    # Includes listings far outside the freshness window, as a real first page does.
    transport.queue_catalog([make_item(i, photo_ts=now - i * 3600) for i in range(1, 31)])

    await poller.tick()

    assert await repo.outbox_depth() == 1
    assert len(await repo.known_item_ids(list(range(1, 31)))) == 30

    queued = await repo.claim_batch(destination_id, 50)
    assert [n.item.item_id for n in queued] == [1], "the newest listing, and only that"


async def test_only_listings_newer_than_the_last_check_are_announced(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, work = await make_poller(transport, repo, settings, db=db)
    destination_id = await repo.add_destination(kind="webhook", name="test", config={"url": "x"})
    await repo.route(poller.query.id, destination_id)

    now = int(time.time())
    transport.queue_catalog([make_item(1, photo_ts=now - 300)])
    await poller.tick()
    assert await repo.outbox_depth() == 0

    # The same listing again, plus one genuinely new one.
    transport.queue_catalog([make_item(2, photo_ts=now - 10), make_item(1, photo_ts=now - 300)])
    await poller.tick()

    assert await repo.outbox_depth() == 1
    assert work.is_set()


async def test_a_blocked_request_backs_off_and_replaces_the_session(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    clock = FakeClock()
    poller, _ = await make_poller(transport, repo, settings, db=db, clock=clock)
    await poller.tick()  # a first, successful check establishes a session
    transport.queue_status(403, "Forbidden")
    homepage_visits_before = sum(1 for r in transport.requests if "/api/v2/" not in r["url"])

    delay = await poller.tick()

    state = await repo.get_state(poller.query.id)
    assert state.last_status == "http_403"
    assert state.count_403 == 1
    assert delay > settings.poll_default_interval_s, "a block must slow us down, not speed us up"

    homepage_visits_now = sum(1 for r in transport.requests if "/api/v2/" not in r["url"])
    assert homepage_visits_now == homepage_visits_before, (
        "being refused must not trigger another handshake on the spot; that is the one "
        "request guaranteed to make the block longer"
    )

    # Once the backoff has passed, the search comes back with a fresh session and keeps
    # running rather than dying.
    clock.advance(delay + 1)
    transport.queue_catalog([])
    assert await poller.tick() > 0
    assert (await repo.get_state(poller.query.id)).last_status == "ok"
    homepage_visits_after = sum(1 for r in transport.requests if "/api/v2/" not in r["url"])
    assert homepage_visits_after > homepage_visits_before, (
        "the next check should start a fresh session rather than reuse the refused one"
    )


async def test_a_refusal_during_the_handshake_itself_is_a_backoff_not_a_crash(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    """The homepage answering without a session cookie is the DataDome challenge page.

    That used to escape the poller as an unhandled exception, kill its task, and have the
    supervisor restart it fifteen seconds later — every search on the site hitting the
    homepage four times a minute until the address was blocked outright.
    """
    clock = FakeClock()
    poller, _ = await make_poller(transport, repo, settings, db=db, clock=clock)
    transport.queue_root(
        Response(status_code=200, text="<html>challenge</html>", headers={}, cookies={})
    )

    delay = await poller.tick()  # must not raise

    state = await repo.get_state(poller.query.id)
    assert state.last_status == "http_403"
    assert delay > settings.poll_default_interval_s

    # A second refusal at the handshake, still no crash, and a longer wait.
    clock.advance(delay + 1)
    transport.queue_root(
        Response(status_code=200, text="<html>challenge</html>", headers={}, cookies={})
    )
    assert await poller.tick() > delay


async def test_one_refusal_holds_every_search_on_that_site(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    clock = FakeClock()
    sessions = SessionManager(db, transport, cooldown=SiteCooldown(clock=clock))
    client = VintedClient(transport, sessions)

    async def add(name: str) -> Poller:
        query_id = await repo.add_query(
            name=name,
            url=f"https://www.vinted.fr/catalog?search_text={name}",
            tld="fr",
            params={"search_text": name},
            poll_interval_s=60,
        )
        query = await repo.get_query(query_id)
        assert query is not None
        return Poller(
            query,
            repo=repo,
            client=client,
            sessions=sessions,
            settings=settings,
            stop=asyncio.Event(),
        )

    first, second = await add("first"), await add("second")

    transport.queue_status(403, "Forbidden")
    hold = await first.tick()
    requests_before = len(transport.requests)

    wait = await second.tick()

    assert len(transport.requests) == requests_before, (
        "the second search must not go and discover the block for itself"
    )
    assert wait >= hold * 0.9
    assert (await repo.get_state(second.query.id)).last_status is None, (
        "waiting out someone else's backoff is not a failure of this search"
    )

    clock.advance(hold + 10)
    transport.queue_catalog([])
    assert await second.tick() > 0
    assert (await repo.get_state(second.query.id)).last_status == "ok"


async def test_the_first_refusal_on_a_site_is_announced_once(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    clock = FakeClock()
    announcements: list[str] = []

    async def announce(message: Any) -> None:
        # The dispatcher renders per destination language; here, English and Slovak.
        announcements.append(message(i18n.get("en")) if callable(message) else message)
        if callable(message):
            assert "odmieta požiadavky" in message(i18n.get("sk"))

    poller, _ = await make_poller(transport, repo, settings, db=db, clock=clock, announce=announce)

    transport.queue_status(403, "Forbidden")
    delay = await poller.tick()
    assert len(announcements) == 1
    assert "vinted.fr" in announcements[0]

    # Still inside the hold: a second refusal from the same site is not news.
    clock.advance(delay / 2)
    assert await poller.tick() > 0
    assert len(announcements) == 1


async def test_a_blocked_proxy_is_set_aside_and_the_next_one_is_used(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any, tmp_path: Any
) -> None:
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("http://one.test\nhttp://two.test")
    rotation = ProxyRotation.from_file(proxy_file)

    query_id = await repo.add_query(
        name="proxied",
        url="https://www.vinted.fr/catalog?search_text=x",
        tld="fr",
        params={"search_text": "x"},
        poll_interval_s=60,
    )
    query = await repo.get_query(query_id)
    assert query is not None

    sessions = SessionManager(db, TransportPool(lambda _proxy: transport), proxies=rotation)
    poller = Poller(
        query,
        repo=repo,
        client=VintedClient(None, sessions),
        sessions=sessions,
        settings=settings,
        stop=asyncio.Event(),
    )

    transport.queue_status(403, "Forbidden")
    await poller.tick()

    assert rotation.available() == 1, "the route that was refused should sit out"


async def test_backoff_grows_with_repeated_blocks(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    clock = FakeClock()
    poller, _ = await make_poller(transport, repo, settings, db=db, clock=clock)

    delays = []
    for _ in range(3):
        transport.queue_status(403, "Forbidden")
        delays.append(await poller.tick())
        clock.advance(delays[-1] + 1)

    assert delays[0] < delays[1] < delays[2]


async def test_rate_limiting_waits_exactly_as_long_as_asked(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    poller, _ = await make_poller(transport, repo, settings, db=db)
    transport.queue_status(429, "slow down", **{"retry-after": "42"})

    delay = await poller.tick()

    assert delay == 42.0
    state = await repo.get_state(poller.query.id)
    assert state.count_429 == 1
    assert await db.fetch_one("SELECT 1 FROM sessions WHERE tld = 'fr'") is not None, (
        "a rate limit is not a reason to throw away a working session"
    )


async def test_an_expired_token_is_replaced_and_the_next_check_works(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, _ = await make_poller(transport, repo, settings, db=db)

    # Vinted reports this inside a 200 body as well as with a 401.
    transport.queue(_json_response({"code": 100, "message": "invalid_authentication_token"}))
    delay = await poller.tick()

    assert (await repo.get_state(poller.query.id)).last_status == "http_401"
    assert delay <= 60, "a stale token should be retried promptly, not backed off"
    assert await db.fetch_one("SELECT 1 FROM sessions WHERE tld = 'fr'") is None

    transport.queue_catalog([make_item(1, photo_ts=int(time.time()))])
    await poller.tick()
    assert (await repo.get_state(poller.query.id)).last_status == "ok"


async def test_a_response_that_is_not_a_catalog_is_reported_not_announced(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    poller, work = await make_poller(transport, repo, settings, db=db)
    transport.queue_status(200, "<html>are you a robot?</html>")

    await poller.tick()

    state = await repo.get_state(poller.query.id)
    assert state.last_status == "malformed"
    assert state.last_error is not None
    assert await repo.outbox_depth() == 0
    assert not work.is_set()


async def test_listings_over_the_total_price_limit_are_skipped(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, _ = await make_poller(transport, repo, settings, db=db, max_total_price="20")
    destination_id = await repo.add_destination(kind="webhook", name="test", config={"url": "x"})
    await repo.route(poller.query.id, destination_id)

    now = int(time.time())
    transport.queue_catalog([make_item(1, photo_ts=now - 600)])
    await poller.tick()

    # 18.00 asking price becomes 20.50 with buyer protection, which is over the limit even
    # though Vinted's own price filter would have let it through.
    transport.queue_catalog(
        [make_item(2, photo_ts=now, price="18.0"), make_item(3, photo_ts=now, price="10.0")]
    )
    await poller.tick()

    queued = await repo.claim_batch(destination_id, 10)
    assert [n.item.item_id for n in queued] == [3]


async def test_a_restart_does_not_resend_what_was_already_sent(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, _ = await make_poller(transport, repo, settings, db=db)
    destination_id = await repo.add_destination(kind="webhook", name="test", config={"url": "x"})
    await repo.route(poller.query.id, destination_id)

    now = int(time.time())
    transport.queue_catalog([make_item(1, photo_ts=now - 500)])
    await poller.tick()

    transport.queue_catalog([make_item(2, photo_ts=now - 5)])
    await poller.tick()
    first_batch = await repo.claim_batch(destination_id, 10)
    await repo.mark_sent([n.outbox_id for n in first_batch])

    # A fresh poller over the same database, as if the process had restarted.
    restarted, _ = poller_for(poller.query, transport, repo, settings, db=db)
    transport.queue_catalog([make_item(2, photo_ts=now - 5)])
    await restarted.tick()

    assert await repo.outbox_depth() == 0


def _json_response(payload: dict[str, Any]) -> Response:
    return Response(status_code=200, text=json.dumps(payload), headers={}, cookies={})


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_transient(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any, status: int
) -> None:
    poller, _ = await make_poller(transport, repo, settings, db=db)
    transport.queue_status(status, "oops")

    delay = await poller.tick()

    assert delay > 0
    assert (await repo.get_state(poller.query.id)).last_status == "network"


# --- Price drops ---------------------------------------------------------------------


async def test_a_listing_that_gets_cheaper_is_announced_again(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    poller, work = await make_poller(transport, repo, settings, db=db)
    destination_id = await repo.add_destination(
        kind="webhook", name="t", config={"url": "https://example.test/hook"}
    )
    await repo.route(poller.query.id, destination_id)
    now = int(time.time())

    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="50.0")])
    await poller.tick()  # first run: recorded, nothing announced (silent mode)
    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="50.0")])
    await poller.tick()  # same price: nothing
    assert await repo.outbox_depth() == 0

    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="40.0")])
    await poller.tick()  # 20% off: news

    assert await repo.outbox_depth() == 1
    assert work.is_set()
    batch = await repo.claim_batch(destination_id, 10)
    assert batch[0].is_price_drop
    assert batch[0].previous_price is not None
    assert batch[0].item.price == Decimal("40.0")
    assert batch[0].headline().startswith("Price drop -20%")
    await repo.mark_sent([batch[0].outbox_id])

    # The stored price moved with it, so the same drop is not reported twice, and a
    # smaller wobble after that is beneath notice.
    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="40.0")])
    await poller.tick()
    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="38.0")])
    await poller.tick()
    assert await repo.outbox_depth() == 0


async def test_price_drop_tracking_can_be_switched_off(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    settings = settings.model_copy(update={"price_drop_min_percent": 0})
    poller, _ = await make_poller(transport, repo, settings, db=db)
    now = int(time.time())
    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="50.0")])
    await poller.tick()
    transport.queue_catalog([make_item(1, photo_ts=now - 5, price="10.0")])
    await poller.tick()
    assert await repo.outbox_depth() == 0


async def test_a_hold_is_written_down_and_shows_as_cooling(
    transport: ScriptedTransport, repo: Repo, settings: Settings, db: Any
) -> None:
    clock = FakeClock()
    poller, _ = await make_poller(transport, repo, settings, db=db, clock=clock)
    transport.queue_status(403, "Forbidden")
    await poller.tick()

    snapshot = await health.snapshot(repo)
    (search,) = snapshot.searches
    assert search.state == "cooling"
    assert search.cooling_until is not None and search.cooling_until > time.time()


# --- Market percentile filter ----------------------------------------------------------


async def test_the_market_filter_keeps_only_the_cheapest_share_once_it_knows_the_market(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    query_id = await repo.add_query(
        name="cheap only",
        url="https://www.vinted.fr/catalog?search_text=x",
        tld="fr",
        params={"search_text": "x"},
        poll_interval_s=60,
        max_market_percentile=25,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    poller, _ = poller_for(query, transport, repo, settings, db=db)
    destination = await repo.add_destination(kind="ntfy", name="t", config={"topic": "t"})
    await repo.route(query_id, destination)
    now = int(time.time())

    # First check seeds a market of twenty prices, 10..29 (silent first run).
    transport.queue_catalog(
        [make_item(i, photo_ts=now - 600, price=str(10 + i)) for i in range(20)]
    )
    await poller.tick()
    assert await repo.outbox_depth() == 0

    # A listing at 12 is in the cheapest quarter; one at 25 is not.
    transport.queue_catalog(
        [
            make_item(100, photo_ts=now - 5, price="12.0"),
            make_item(101, photo_ts=now - 4, price="25.0"),
        ]
    )
    await poller.tick()

    (queued,) = await repo.claim_batch(destination, 10)
    assert queued.item.item_id == 100
    assert queued.market_percentile is not None and queued.market_percentile <= 25
    assert queued.market_n == 22
    market_line = queued.market_line()
    assert market_line is not None and "similar listings" in market_line


async def test_the_market_filter_waits_until_there_is_a_market(
    transport: ScriptedTransport,
    repo: Repo,
    settings: Settings,
    db: Any,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    query_id = await repo.add_query(
        name="cheap only",
        url="https://www.vinted.fr/catalog?search_text=x",
        tld="fr",
        params={"search_text": "x"},
        poll_interval_s=60,
        max_market_percentile=25,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    poller, _ = poller_for(query, transport, repo, settings, db=db)
    destination = await repo.add_destination(kind="ntfy", name="t", config={"topic": "t"})
    await repo.route(query_id, destination)
    now = int(time.time())

    transport.queue_catalog([make_item(1, photo_ts=now - 600, price="10.0")])
    await poller.tick()
    transport.queue_catalog([make_item(2, photo_ts=now - 5, price="99.0")])
    await poller.tick()

    assert await repo.outbox_depth() == 1, "too few points to judge, so nothing is withheld"
