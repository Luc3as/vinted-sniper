"""The outbox: what happens between finding a listing and it arriving."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from vinted_sniper.botctl.telegram_bot import _chat_is_paired, claim_pairing, create_pairing
from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Query, Repo
from vinted_sniper.deliver.dispatcher import MAX_ATTEMPTS, Dispatcher
from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.vinted.models import Item


def listing(item_id: int) -> Item:
    return Item(
        item_id=item_id,
        tld="fr",
        title=f"Item {item_id}",
        url=f"https://www.vinted.fr/items/{item_id}",
        price=Decimal("10.00"),
        total_price=Decimal("11.70"),
        currency="EUR",
        photo_ts=int(time.time()),
    )


async def a_search(repo: Repo, min_enrich_score: int | None = None) -> Query:
    query_id = await repo.add_query(
        name="test",
        url="https://www.vinted.fr/catalog?search_text=x",
        tld="fr",
        params={},
        poll_interval_s=60,
        min_enrich_score=min_enrich_score,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    return query


class FakeEndpoint:
    def __init__(self, *responses: httpx.Response) -> None:
        self.calls = 0
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json={"ok": True})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def make_dispatcher(repo: Repo, settings: Settings, endpoint: FakeEndpoint) -> Dispatcher:
    return Dispatcher(
        repo=repo,
        settings=settings,
        stop=asyncio.Event(),
        work_available=asyncio.Event(),
        client=endpoint.client(),
    )


async def test_a_queued_notification_is_delivered_once(repo: Repo, settings: Settings) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    endpoint = FakeEndpoint()
    sent = await make_dispatcher(repo, settings, endpoint).drain()

    assert sent == 1
    assert endpoint.calls == 1
    assert await repo.outbox_depth() == 0

    # A second pass has nothing left to do.
    assert await make_dispatcher(repo, settings, endpoint).drain() == 0
    assert endpoint.calls == 1


async def test_the_same_listing_is_never_queued_twice_for_one_destination(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )

    await repo.record_new_items(query, [listing(1)], [destination_id])
    await repo.record_new_items(query, [listing(1)], [destination_id])

    assert await repo.outbox_depth() == 1


async def test_one_listing_reaches_every_destination_it_is_routed_to(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    first = await repo.add_destination(
        kind="webhook", name="a", config={"url": "https://a.test/hook"}
    )
    second = await repo.add_destination(
        kind="webhook", name="b", config={"url": "https://b.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [first, second])

    endpoint = FakeEndpoint()
    assert await make_dispatcher(repo, settings, endpoint).drain() == 2


async def test_a_failing_endpoint_is_retried_then_given_up_on(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    endpoint = FakeEndpoint(*[httpx.Response(500) for _ in range(MAX_ATTEMPTS + 2)])
    dispatcher = make_dispatcher(repo, settings, endpoint)

    for _ in range(MAX_ATTEMPTS + 1):
        await dispatcher.drain()
        # Retries are scheduled a little ahead; pretend that time has passed.
        await _make_everything_due(repo)

    assert await repo.outbox_depth() == 0, "it must not retry forever"
    row = await _outbox_row(repo)
    assert row["status"] == "failed"


async def test_a_dead_destination_is_switched_off(repo: Repo, settings: Settings) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="discord", name="test", config={"webhook_url": "https://discord.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    endpoint = FakeEndpoint(httpx.Response(404, json={"message": "Unknown Webhook"}))
    await make_dispatcher(repo, settings, endpoint).drain()

    destination = await repo.get_destination(destination_id)
    assert destination is not None
    assert destination.active is False
    assert await repo.outbox_depth() == 0, "queued notifications for it are cleared out"


async def test_notifications_stranded_by_a_crash_are_picked_back_up(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    # Claim it, then vanish without acknowledging — what a kill -9 looks like.
    claimed = await repo.claim_batch(destination_id, 10)
    assert len(claimed) == 1
    assert await repo.outbox_depth() == 1

    recovered = await repo.recover_leases()
    assert recovered == 1

    endpoint = FakeEndpoint()
    assert await make_dispatcher(repo, settings, endpoint).drain() == 1


async def test_stale_notifications_are_dropped_rather_than_delivered_late(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])
    await _age_outbox(repo, minutes=settings.outbox_expiry_minutes + 10)

    endpoint = FakeEndpoint()
    sent = await make_dispatcher(repo, settings, endpoint).drain()

    assert sent == 0
    assert endpoint.calls == 0, "nobody wants an alert about a listing from two hours ago"
    assert await repo.outbox_depth() == 0


async def test_notifications_are_delivered_in_the_order_they_were_found(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="webhook", name="test", config={"url": "https://example.test/hook"}
    )
    for item_id in (1, 2, 3):
        await repo.record_new_items(query, [listing(item_id)], [destination_id])

    batch = await repo.claim_batch(destination_id, 10)

    assert [n.item.item_id for n in batch] == [1, 2, 3]


async def test_a_telegram_destination_without_a_token_is_reported_not_retried(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="telegram", name="test", config={"chat_id": "123"}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    endpoint = FakeEndpoint()
    await make_dispatcher(repo, settings, endpoint).drain()

    destination = await repo.get_destination(destination_id)
    assert destination is not None
    assert destination.active is False


async def _outbox_row(repo: Repo) -> Any:
    db = repo._db  # the test asserts on stored state on purpose
    row = await db.fetch_one("SELECT * FROM outbox LIMIT 1")
    assert row is not None
    return row


async def _make_everything_due(repo: Repo) -> None:
    db = repo._db
    await db.execute("UPDATE outbox SET next_attempt_at = 0 WHERE status = 'pending'")


async def _age_outbox(repo: Repo, *, minutes: int) -> None:
    db = repo._db
    await db.execute("UPDATE outbox SET created_at = ?", (int(time.time()) - minutes * 60,))


@pytest.mark.parametrize("kind", ["discord", "telegram", "webhook", "ntfy"])
async def test_every_destination_type_can_be_created(repo: Repo, kind: str) -> None:
    config = {
        "discord": {"webhook_url": "https://discord.test/hook"},
        "telegram": {"chat_id": "1"},
        "webhook": {"url": "https://example.test"},
        "ntfy": {"topic": "my-topic"},
    }[kind]

    destination_id = await repo.add_destination(kind=kind, name=kind, config=config)

    destination = await repo.get_destination(destination_id)
    assert destination is not None
    assert destination.kind == kind


async def test_a_telegram_destination_waiting_to_be_paired_is_left_alone(
    repo: Repo, settings: Settings
) -> None:
    """Tapping the pairing link can happen after the first listing turns up.

    A destination nobody has claimed yet is waiting, not misconfigured — switching it off
    would mean the link you are about to tap no longer works.
    """
    query = await a_search(repo)
    destination_id = await repo.add_destination(
        kind="telegram", name="waiting", config={"pairing_code": "abc", "created_at": 0}
    )
    await repo.record_new_items(query, [listing(1)], [destination_id])

    endpoint = FakeEndpoint()
    sent = await make_dispatcher(repo, settings, endpoint).drain()

    assert sent == 0
    destination = await repo.get_destination(destination_id)
    assert destination is not None
    assert destination.active is True, "waiting to be paired is not a failure"
    assert await repo.outbox_depth() == 1, "the notification waits for the chat to be bound"


async def test_connecting_a_chat_drops_the_backlog(repo: Repo) -> None:
    """Whoever just tapped the link wants what happens next, not a hundred old alerts."""
    query = await a_search(repo)
    destination_id, code = await create_pairing(repo, "Telegram")
    await repo.record_new_items(query, [listing(i) for i in range(1, 40)], [destination_id])
    assert await repo.outbox_depth() == 39

    assert await claim_pairing(repo, code, chat_id=4242, thread_id=None) == destination_id

    assert await repo.outbox_depth() == 0
    destination = await repo.get_destination(destination_id)
    assert destination is not None
    assert destination.config["chat_id"] == "4242"
    assert destination.active is True


async def test_asking_for_a_second_pairing_link_reuses_the_waiting_destination(
    repo: Repo,
) -> None:
    """Otherwise the searches stay pointed at the abandoned one and the new link is inert."""
    query = await a_search(repo)
    first_id, _ = await create_pairing(repo, "Telegram")
    await repo.route(query.id, first_id)

    second_id, code = await create_pairing(repo, "Telegram")

    assert second_id == first_id
    assert len(await repo.list_destinations()) == 1
    assert await claim_pairing(repo, code, chat_id=7, thread_id=None) == first_id
    assert await repo.destination_ids_for_query(query.id) == [first_id]


# --- Actions under an alert ----------------------------------------------------------


async def test_skipping_a_seller_from_an_alert_extends_the_search_filter(repo: Repo) -> None:
    query_id = await repo.add_query(
        name="x",
        url="https://www.vinted.fr/catalog?search_text=x",
        tld="fr",
        params={"search_text": "x"},
        poll_interval_s=60,
        blocked_sellers=["already"],
    )

    assert await repo.block_seller(query_id, "Scammer99") is True
    assert await repo.block_seller(query_id, "scammer99") is False, "case-insensitively once"
    assert await repo.block_seller(query_id, "already") is False
    assert await repo.block_seller(9999, "nobody") is False

    query = await repo.get_query(query_id)
    assert query is not None
    assert query.blocked_sellers == ["already", "Scammer99"]


async def test_buttons_are_only_honoured_from_a_paired_chat(repo: Repo) -> None:
    _, code = await create_pairing(repo, "phone")
    assert not await _chat_is_paired(repo, 4242)

    await claim_pairing(repo, code, chat_id=4242, thread_id=None)
    assert await _chat_is_paired(repo, 4242)
    assert not await _chat_is_paired(repo, 1)


# --- Quiet hours ---------------------------------------------------------------------


async def test_a_destination_in_its_quiet_hours_is_left_alone_and_nothing_expires(
    repo: Repo, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = await a_search(repo)
    sleeping = await repo.add_destination(
        kind="webhook",
        name="phone",
        config={"url": "https://example.test/phone"},
        quiet_hours="23:00-07:00",
    )
    awake = await repo.add_destination(
        kind="webhook", name="desk", config={"url": "https://example.test/desk"}
    )
    await repo.record_new_items(query, [listing(1)], [sleeping, awake])
    await _age_outbox(repo, minutes=settings.outbox_expiry_minutes + 10)

    endpoint = FakeEndpoint()
    dispatcher = make_dispatcher(repo, settings, endpoint)

    night = datetime(2026, 9, 6, 3, 0, tzinfo=dispatcher._zone)
    monkeypatch.setattr(dispatcher, "_local_now", lambda: night)
    sent = await dispatcher.drain()

    assert sent == 0, "the desk's copy was stale and dropped; the phone's is held"
    assert await repo.outbox_depth() == 1, "the phone keeps its alert through the night"

    morning = datetime(2026, 9, 6, 7, 30, tzinfo=dispatcher._zone)
    monkeypatch.setattr(dispatcher, "_local_now", lambda: morning)
    sent = await dispatcher.drain()

    assert sent == 1, "the held alert goes out when the window ends, not into the bin"
    assert endpoint.calls == 1
    assert await repo.outbox_depth() == 0


# --- Enrichment loop -----------------------------------------------------------------


async def test_chat_alerts_wait_for_a_verdict_while_the_webhook_fires_at_once(
    repo: Repo, settings: Settings
) -> None:
    settings = settings.model_copy(update={"enrichment_wait_s": 90})
    query = await a_search(repo)
    brain = await repo.add_destination(
        kind="webhook", name="n8n", config={"url": "https://example.test/n8n"}
    )
    phone = await repo.add_destination(
        kind="webhook", name="phone", config={"url": "https://example.test/phone"}
    )
    # A plain webhook is never held; to stand in for a chat destination, hold "phone"
    # by name rather than by kind.
    await repo.record_new_items(
        query, [listing(1)], [brain, phone], hold_s=settings.enrichment_wait_s, never_hold={brain}
    )

    endpoint = FakeEndpoint()
    dispatcher = make_dispatcher(repo, settings, endpoint)
    assert await dispatcher.drain() == 1, "only the brain's copy is due now"
    assert endpoint.calls == 1
    body = json.loads(endpoint_last_request(endpoint).content)
    assert body["items"][0]["enrichment_url"].endswith("/api/items/1/enrichment")
    assert "photo_urls" in body["items"][0]

    # The verdict arrives: the held copy is released immediately, carrying it.
    assert await repo.store_enrichment(
        1, EnrichmentIn(score=88, model="Nike Air Max 90", retail_price=Decimal("140"))
    )
    assert await dispatcher.drain() == 1
    sent = json.loads(endpoint_last_request(endpoint).content)
    assert sent["items"][0]["id"] == 1

    batch_view = await repo.claim_batch(phone, 10)
    assert batch_view == [], "nothing left"


async def test_a_dull_verdict_cancels_the_alert_when_the_search_sets_a_floor(
    repo: Repo, settings: Settings
) -> None:
    settings = settings.model_copy(update={"enrichment_wait_s": 90})
    query = await a_search(repo, min_enrich_score=60)
    phone = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.record_new_items(query, [listing(1)], [phone], hold_s=settings.enrichment_wait_s)

    endpoint = FakeEndpoint()
    dispatcher = make_dispatcher(repo, settings, endpoint)
    assert await dispatcher.drain() == 0, "held for the verdict"

    assert await repo.store_enrichment(1, EnrichmentIn(score=25))
    assert await dispatcher.drain() == 0, "released, judged, and cancelled"
    assert endpoint.calls == 0, "nothing reached the phone"
    assert await repo.outbox_depth() == 0, "cancelled outright, not left pending"


async def test_a_verdict_at_the_floor_still_alerts(repo: Repo, settings: Settings) -> None:
    settings = settings.model_copy(update={"enrichment_wait_s": 90})
    query = await a_search(repo, min_enrich_score=60)
    phone = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.record_new_items(query, [listing(1)], [phone], hold_s=settings.enrichment_wait_s)
    assert await repo.store_enrichment(1, EnrichmentIn(score=60))

    endpoint = FakeEndpoint()
    assert await make_dispatcher(repo, settings, endpoint).drain() == 1
    assert endpoint.calls == 1


async def test_a_missing_verdict_never_blocks_the_alert(repo: Repo, settings: Settings) -> None:
    """The floor gates only what the agent actually judged: a dead or slow agent must
    cost at most the wait, never the alert itself."""
    query = await a_search(repo, min_enrich_score=60)
    phone = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.record_new_items(query, [listing(1)], [phone], hold_s=0)

    endpoint = FakeEndpoint()
    assert await make_dispatcher(repo, settings, endpoint).drain() == 1, "unscored still alerts"
    assert endpoint.calls == 1


async def test_a_verdict_for_an_unknown_listing_is_refused(repo: Repo) -> None:
    assert not await repo.store_enrichment(424242, EnrichmentIn(score=10))


def endpoint_last_request(endpoint: FakeEndpoint) -> httpx.Request:
    return endpoint.requests[-1]


async def test_a_hot_verdict_arriving_late_earns_a_follow_up_but_a_dull_one_does_not(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    phone = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.record_new_items(query, [listing(1), listing(2)], [phone])
    endpoint = FakeEndpoint()
    dispatcher = make_dispatcher(repo, settings, endpoint)
    assert await dispatcher.drain() == 2, "both alerts went out before any verdict"

    assert await repo.store_enrichment(
        1, EnrichmentIn(score=92, verdict="Berte."), followup_min_score=75
    )
    assert await repo.store_enrichment(
        2, EnrichmentIn(score=30, verdict="Nič moc."), followup_min_score=75
    )

    batch = await repo.claim_batch(phone, 10)
    assert [n.item.item_id for n in batch] == [1]
    assert batch[0].is_verdict
    assert batch[0].enrichment is not None and batch[0].enrichment.score == 92
    assert batch[0].headline() == "Verdict is in"


async def test_a_verdict_that_arrives_in_time_does_not_also_send_a_follow_up(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    phone = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.record_new_items(query, [listing(1)], [phone], hold_s=90)

    assert await repo.store_enrichment(1, EnrichmentIn(score=95), followup_min_score=75)

    batch = await repo.claim_batch(phone, 10)
    assert len(batch) == 1 and batch[0].kind == "new", "released, enriched, once"


# --- Market memory and feedback ------------------------------------------------------


async def test_the_webhook_carries_market_position_retail_and_the_buyers_past_verdicts(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    # Thirty days of market: prices 10..49, uniformly.
    observed = [
        Item(
            item_id=1000 + i,
            tld="fr",
            title=f"m{i}",
            url=f"https://www.vinted.fr/items/{1000 + i}",
            price=Decimal(10 + i),
            total_price=Decimal(10 + i),
            currency="EUR",
            condition="Very good",
        )
        for i in range(40)
    ]
    await repo.observe_market(query.id, observed)
    await repo.remember_retail(query.id, "Nike Air Max 90", Decimal("140"), "EUR", "nike.com")

    brain = await repo.add_destination(
        kind="webhook", name="n8n", config={"url": "https://example.test/n8n"}
    )
    cheap = listing(1)  # total 11.70 → among the cheapest
    await repo.record_new_items(query, [cheap], [brain])
    # An earlier verdict the buyer rated.
    await repo.record_new_items(query, [listing(2)], [])
    await repo.store_enrichment(2, EnrichmentIn(score=80, model="Nike Air Max 90"))
    assert await repo.rate_verdict(2, 1)

    endpoint = FakeEndpoint()
    await make_dispatcher(repo, settings, endpoint).drain()
    body = json.loads(endpoint.requests[-1].content)
    sent = body["items"][0]

    assert sent["market"]["n"] == 40
    assert sent["market"]["median"] == 30.0
    assert sent["market"]["this_percentile"] <= 5
    assert sent["market"]["median_same_condition"] is None, "the listing states no condition"
    same = await repo.market_context(query.id, Decimal("11.70"), "very good")
    assert same is not None and same["median_same_condition"] == 30.0
    assert sent["known_retail"][0]["model"] == "Nike Air Max 90"
    assert sent["buyer_feedback"][0]["buyer_said"] == "good deal"
    assert "favourites_per_hour" in sent


async def test_a_verdict_with_a_retail_price_is_remembered_for_the_search(
    repo: Repo,
) -> None:
    query = await a_search(repo)
    await repo.record_new_items(query, [listing(1)], [])
    await repo.store_enrichment(
        1,
        EnrichmentIn(
            model="Nike Air Max 90", retail_price=Decimal("140"), retail_source="nike.com"
        ),
    )
    known = await repo.known_retail(query.id)
    assert known == [
        {"model": "Nike Air Max 90", "price": 140.0, "currency": "EUR", "source": "nike.com"}
    ]


async def test_market_context_is_withheld_until_there_is_enough_of_it(repo: Repo) -> None:
    query = await a_search(repo)
    await repo.observe_market(query.id, [listing(1), listing(2)])
    assert await repo.market_context(query.id, Decimal("11.70"), None) is None


async def test_a_destination_language_travels_with_the_pairing(repo: Repo) -> None:
    destination_id, code = await create_pairing(repo, "Telefón")
    await repo.set_language(destination_id, "sk")
    await claim_pairing(repo, code, chat_id=4242, thread_id=None)

    assert await repo.chat_language(4242) == "sk"
    assert await repo.chat_language(1) is None
    (destination,) = [d for d in await repo.list_destinations() if d.id == destination_id]
    assert destination.language == "sk"


async def test_status_notices_are_rendered_per_destination_language(
    repo: Repo, settings: Settings
) -> None:
    en = await repo.add_destination(
        kind="webhook", name="en", config={"url": "https://example.test/en"}, notify_status=True
    )
    sk = await repo.add_destination(
        kind="webhook",
        name="sk",
        config={"url": "https://example.test/sk"},
        notify_status=True,
        language="sk",
    )
    endpoint = FakeEndpoint()
    dispatcher = make_dispatcher(repo, settings, endpoint)

    await dispatcher.notify_status(lambda t: t("Running."))

    bodies = {json.loads(r.content)["status"] for r in endpoint.requests}
    assert bodies == {"Running.", "Beží."}
    assert en != sk


async def test_the_agent_is_told_which_language_the_buyer_reads(
    repo: Repo, settings: Settings
) -> None:
    query = await a_search(repo)
    brain = await repo.add_destination(
        kind="webhook", name="n8n", config={"url": "https://example.test/n8n"}
    )
    phone = await repo.add_destination(
        kind="ntfy", name="phone", config={"topic": "t"}, language="sk"
    )
    await repo.route(query.id, brain)
    await repo.route(query.id, phone)
    await repo.record_new_items(query, [listing(1)], [brain])

    endpoint = FakeEndpoint()
    await make_dispatcher(repo, settings, endpoint).drain()

    body = json.loads(endpoint.requests[-1].content)
    assert body["items"][0]["reader_language"] == "sk"
    assert await repo.reader_language_for_query(9999) == "en", "no readers: default"
