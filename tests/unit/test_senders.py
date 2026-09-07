"""What we actually send, and what we do when the platform pushes back."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from vinted_sniper.db.repo import PendingNotification
from vinted_sniper.deliver.discord import DiscordSender
from vinted_sniper.deliver.ratelimit import Gate, TokenBucket
from vinted_sniper.deliver.telegram import TelegramSender, inline_actions
from vinted_sniper.enrichment import Enrichment
from vinted_sniper.vinted.models import Item


def notification(item_id: int, **kwargs: Any) -> PendingNotification:
    defaults: dict[str, Any] = {
        "item_id": item_id,
        "tld": "fr",
        "title": f"Item {item_id}",
        "url": f"https://www.vinted.fr/items/{item_id}",
        "price": Decimal("15.00"),
        "total_price": Decimal("16.45"),
        "currency": "EUR",
        "brand": "Nike",
        "size": "M",
        "condition": "Very good",
        "photo_url": f"https://images.vinted.net/{item_id}.jpeg",
        "photo_ts": 1_760_000_000,
        "seller_login": "sneakerfan",
        "seller_id": 12_345_678,
        "seller_rating": 0.96,
        "seller_feedback_count": 128,
    }
    defaults.update(kwargs)
    return PendingNotification(
        outbox_id=item_id,
        destination_id=1,
        query_id=1,
        query_name="my search",
        attempts=0,
        item=Item(**defaults),
        detected_at=1_760_000_100,
    )


class Recorder:
    """Captures requests and replays scripted responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(204)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def payload(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].content)


def fast_bucket() -> TokenBucket:
    """A bucket that never actually waits, so tests do not either."""
    return TokenBucket(1000.0, capacity=1000.0)


# --- Discord -----------------------------------------------------------------------


async def test_a_few_listings_arrive_one_rich_message_each() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    result = await sender.send([notification(1), notification(2)])

    assert result.delivered == [1, 2]
    assert len(recorder.requests) == 2
    payload = recorder.payload(0)
    assert payload["username"] == "Vinted Sniper"
    assert payload["avatar_url"].endswith(".png")
    assert len(payload["embeds"]) == 1


async def test_the_embed_reads_like_a_listing_card() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        dashboard_url="http://127.0.0.1:8000",
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    await sender.send([notification(1)])

    embed = recorder.payload()["embeds"][0]
    assert embed["author"]["name"] == "New match • my search"
    assert embed["title"] == "Item 1"
    assert embed["url"] == "https://www.vinted.fr/items/1"
    assert embed["description"] == (
        "**[View item](https://www.vinted.fr/items/1)**"
        "  •  [Dashboard](http://127.0.0.1:8000)"
        "  •  [Seller](https://www.vinted.fr/member/12345678)"
    )
    assert embed["image"]["url"] == "https://images.vinted.net/1.jpeg"
    assert embed["footer"]["text"] == "Vinted Sniper • vinted.fr"
    assert embed["timestamp"].startswith("2025-10-09")

    by_name = {field["name"]: field["value"] for field in embed["fields"]}
    assert by_name["Location"] == "🇫🇷 FR"
    assert by_name["Seller rating"] == "⭐ 4.8 (128)"
    assert by_name["Seller"] == "@sneakerfan"
    assert by_name["Detected"] == "<t:1760000100:R>"


async def test_links_shrink_to_what_actually_exists() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    await sender.send([notification(1, seller_id=None, seller_login=None, seller_rating=None)])

    embed = recorder.payload()["embeds"][0]
    assert embed["description"] == "**[View item](https://www.vinted.fr/items/1)**", (
        "no dashboard configured and no seller id means no dead links"
    )
    names = [field["name"] for field in embed["fields"]]
    assert "Seller" not in names and "Seller rating" not in names


async def test_many_listings_are_stacked_into_one_message() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    result = await sender.send([notification(i) for i in range(1, 7)])

    assert len(recorder.requests) == 1, "six separate messages would be a wall of noise"
    assert len(result.delivered) == 6
    embeds = recorder.payload()["embeds"]
    assert len(embeds) == 6
    assert all("[View item]" in embed["description"] for embed in embeds), (
        "stacked embeds must each stay self-contained"
    )


async def test_every_embed_carries_its_own_listing_link() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    await sender.send([notification(i) for i in range(1, 6)])

    urls = [embed["url"] for embed in recorder.payload()["embeds"]]
    assert len(set(urls)) == 5, "Discord folds together embeds that share a URL"


async def test_the_total_price_is_what_gets_shown() -> None:
    recorder = Recorder()
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    await sender.send([notification(1)])

    price_field = recorder.payload()["embeds"][0]["fields"][0]
    assert price_field["value"] == "**15.00 EUR**\n16.45 EUR total"


async def test_a_deleted_webhook_is_switched_off_rather_than_retried() -> None:
    recorder = Recorder(httpx.Response(404, json={"message": "Unknown Webhook"}))
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    result = await sender.send([notification(1)])

    assert result.permanent_error is not None
    assert result.retry == [], "retrying a dead webhook spends the allowance that protects the rest"


async def test_rate_limiting_asks_for_the_wait_discord_specified() -> None:
    recorder = Recorder(httpx.Response(429, json={"retry_after": 3.5, "global": False}))
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    result = await sender.send([notification(1)])

    assert result.retry == [1]
    assert result.retry_after_s == 3.5
    assert result.pause_all_for_s is None


async def test_a_global_rate_limit_pauses_every_destination() -> None:
    recorder = Recorder(
        httpx.Response(
            429,
            json={"retry_after": 10.0, "global": True},
            headers={"x-ratelimit-scope": "global"},
        )
    )
    sender = DiscordSender(
        {"webhook_url": "https://discord.test/hook"},
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    result = await sender.send([notification(1)])

    assert result.pause_all_for_s == 10.0


# --- Telegram ----------------------------------------------------------------------


async def test_telegram_sends_one_message_per_listing_with_a_photo_preview() -> None:
    recorder = Recorder(*[httpx.Response(200, json={"ok": True})] * 2)
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    result = await sender.send([notification(1), notification(2)])

    assert result.delivered == [1, 2]
    payload = recorder.payload(0)
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"]["prefer_large_media"] is True
    first_row = payload["reply_markup"]["inline_keyboard"][0]
    assert first_row[0]["url"].endswith("/items/1")
    assert [b["text"] for b in first_row] == ["Open listing", "Seller profile"]
    assert first_row[1]["url"].endswith("/member/12345678")
    assert all("want_it" not in b["url"] and "transaction" not in b["url"] for b in first_row)


async def test_telegram_alerts_carry_skip_seller_and_pause_buttons() -> None:
    recorder = Recorder(httpx.Response(200, json={"ok": True}))
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    await sender.send([notification(1)])

    actions = recorder.payload(0)["reply_markup"]["inline_keyboard"][1]
    data = [button["callback_data"] for button in actions]
    query_id = notification(1).query_id
    assert f"bs:{query_id}:{notification(1).item.seller_login}" in data
    assert f"ps:{query_id}" in data


def test_inline_actions_stay_inside_telegrams_callback_limit() -> None:
    row = inline_actions(7, "x" * 80)
    assert [b["text"] for b in row] == ["⏸ Pause search"], "an oversized login drops its button"
    assert inline_actions(None, "someone") == []


@pytest.mark.parametrize(
    "title",
    [
        "Nike Air Max 90 (2023) — 100% authentic!",
        "T-shirt <script>alert(1)</script>",
        "Vintage & rare • size M",
        "Levi's 501 W32/L34 [new]",
    ],
)
async def test_hostile_titles_do_not_break_the_message(title: str) -> None:
    recorder = Recorder(httpx.Response(200, json={"ok": True}))
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    result = await sender.send([notification(1, title=title)])

    assert result.delivered == [1]
    text = recorder.payload()["text"]
    assert "<script>" not in text
    assert "&lt;" in text or "<b>" in text


async def test_a_burst_collapses_into_a_digest() -> None:
    recorder = Recorder(*[httpx.Response(200, json={"ok": True})] * 6)
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    result = await sender.send([notification(i) for i in range(1, 9)])

    assert len(result.delivered) == 8
    assert len(recorder.requests) == 6, "five listings, then one digest for the rest"
    assert "3 more matches" in recorder.payload(5)["text"]


async def test_a_blocked_bot_stops_being_retried() -> None:
    recorder = Recorder(
        httpx.Response(
            403,
            json={"ok": False, "description": "Forbidden: bot was blocked by the user"},
        )
    )
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    result = await sender.send([notification(1)])

    assert result.permanent_error is not None
    assert result.retry == []


async def test_telegram_rate_limits_are_honoured_exactly() -> None:
    recorder = Recorder(
        httpx.Response(
            429,
            json={
                "ok": False,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )
    )
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )

    result = await sender.send([notification(1)])

    assert result.retry_after_s == 7.0
    assert result.retry == [1]


async def test_listing_times_are_shown_in_the_reader_timezone() -> None:
    """The timestamp on an alert should read as wall-clock time where the reader lives,
    not as UTC with a suffix nobody does the arithmetic on."""
    recorder = Recorder(httpx.Response(200, json={"ok": True}))
    sender = TelegramSender(
        {"chat_id": "123"},
        bot_token="t",
        client=recorder.client(),
        bucket=fast_bucket(),
        zone=ZoneInfo("Europe/Bratislava"),
    )

    # photo_ts 1_760_000_000 is 08:53 UTC on a summer-time day: 10:53 in Bratislava.
    await sender.send([notification(1)])

    text = recorder.payload()["text"]
    assert "10:53" in text
    assert "UTC" not in text


async def test_forum_topics_are_supported() -> None:
    recorder = Recorder(httpx.Response(200, json={"ok": True}))
    sender = TelegramSender(
        {"chat_id": "-100123", "message_thread_id": "42"},
        bot_token="t",
        client=recorder.client(),
        bucket=fast_bucket(),
    )

    await sender.send([notification(1)])

    assert recorder.payload()["message_thread_id"] == "42"


# --- Pacing ------------------------------------------------------------------------


async def test_the_bucket_spaces_requests_out() -> None:
    clock = {"now": 0.0}
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    bucket = TokenBucket(2.0, capacity=1.0, clock=lambda: clock["now"], sleep=sleep)

    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()

    assert slept, "a burst past the capacity has to wait"
    assert sum(slept) == pytest.approx(1.0, abs=0.01), "two per second means half a second each"


def test_a_closed_gate_reopens_on_its_own() -> None:
    clock = {"now": 100.0}
    gate = Gate(clock=lambda: clock["now"])

    assert gate.wait_s == 0.0
    gate.close_for(5.0)
    assert gate.wait_s == pytest.approx(5.0)

    clock["now"] += 5.1
    assert gate.wait_s == 0.0


async def test_telegram_headlines_a_hot_deal_and_mutes_a_dull_one() -> None:
    recorder = Recorder(*[httpx.Response(200, json={"ok": True})] * 2)
    sender = TelegramSender(
        {"chat_id": "123"}, bot_token="t", client=recorder.client(), bucket=fast_bucket()
    )
    hot = Enrichment(
        score=91,
        model="Nike Air Max 90",
        retail_price=Decimal("140"),
        retail_source="nike.com",
        matches_query=True,
        risk=None,
        verdict="Real pair, great price.",
        enriched_at=1,
    )
    dull = Enrichment(
        score=12,
        model=None,
        retail_price=None,
        retail_source=None,
        matches_query=False,
        risk="stock photos only",
        verdict=None,
        enriched_at=1,
    )
    await sender.send([replace(notification(1), enrichment=hot)])
    await sender.send([replace(notification(2), enrichment=dull)])

    first, second = recorder.payload(0), recorder.payload(1)
    assert first["text"].startswith("🔥 <b>HOT DEAL</b> · deal 91/100 · retail ~140 EUR · -88%")
    thumbs = first["reply_markup"]["inline_keyboard"][-1]
    assert [b["callback_data"] for b in thumbs] == ["fb:1:1", "fb:1:-1"]
    assert "Looks like: Nike Air Max 90" in first["text"]
    assert "disable_notification" not in first
    assert second["disable_notification"] is True
    assert "not the model searched for" in second["text"]
    assert "risk: stock photos only" in second["text"]


async def test_a_slovak_destination_gets_a_slovak_alert() -> None:
    recorder = Recorder(httpx.Response(200, json={"ok": True}))
    sender = TelegramSender(
        {"chat_id": "123"},
        bot_token="t",
        client=recorder.client(),
        bucket=fast_bucket(),
        language="sk",
    )
    hot = Enrichment(
        score=91,
        model="Nike Air Max 90",
        retail_price=Decimal("140"),
        retail_source="nike.com",
        matches_query=True,
        risk=None,
        verdict="Berte.",
        enriched_at=1,
    )
    pending = replace(notification(1), enrichment=hot, market_percentile=12, market_n=312)

    await sender.send([pending])

    payload = recorder.payload(0)
    text = payload["text"]
    assert text.startswith("🔥 <b>TOP PONUKA</b> · skóre 91/100 · v obchode ~140 EUR")
    assert "s ochranou kupujúceho" in text
    assert "lacnejší ako 88 % z 312 podobných inzerátov" in text
    assert "Predajca: sneakerfan" in text
    assert "Vyzerá to na: Nike Air Max 90" in text
    buttons = [b["text"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert "Otvoriť inzerát" in buttons
    assert "🚫 Preskočiť predajcu" in buttons
    assert "👍 Trafil sa" in buttons
