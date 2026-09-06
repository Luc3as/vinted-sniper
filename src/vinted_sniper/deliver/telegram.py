"""Telegram delivery.

Sent as HTML rather than MarkdownV2 on purpose. MarkdownV2 requires eighteen characters to
be escaped anywhere they appear, and Vinted titles are full of them — a stray `.` or `!`
turns into a hard send failure rather than a formatting wobble. HTML needs three.

The photo rides along as a link preview instead of an upload. That keeps the full message
length and the buttons, both of which a photo message would cost us, and saves fetching the
image at all.
"""

from __future__ import annotations

import html
from typing import Any

import httpx

from vinted_sniper.db.repo import PendingNotification
from vinted_sniper.deliver.base import SendResult, require
from vinted_sniper.deliver.ratelimit import TokenBucket
from vinted_sniper.enrichment import Enrichment
from vinted_sniper.log import get_logger
from vinted_sniper.vinted.models import Item

log = get_logger(__name__)

API_ROOT = "https://api.telegram.org"

# Telegram asks for no more than one message a second in a single chat.
MESSAGES_PER_S = 1.0

# Past this many listings at once, the rest are folded into one digest. A chat that gets
# twenty separate alerts in a minute is unreadable, and it is also how you get throttled.
DIGEST_THRESHOLD = 5

MAX_MESSAGE_CHARS = 4096

# Telegram states these plainly, so they are worth acting on rather than retrying.
_PERMANENT_MARKERS = (
    "bot was blocked by the user",
    "chat not found",
    "user is deactivated",
    "bot was kicked",
    "have no rights to send",
)


class TelegramSender:
    """Posts listings to one Telegram chat, group, or forum topic."""

    kind = "telegram"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        bot_token: str,
        highlight_score: int = 75,
        silent_below: int = 40,
        client: httpx.AsyncClient | None = None,
        bucket: TokenBucket | None = None,
    ) -> None:
        self._chat_id = str(config.get("chat_id") or "").strip()
        if not self._chat_id:
            require(config, "chat_id", self.kind)
        self._thread_id = config.get("message_thread_id")
        self._token = bot_token
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._bucket = bucket or TokenBucket(MESSAGES_PER_S, capacity=2)
        self._highlight_score = highlight_score
        self._silent_below = silent_below

    @property
    def max_batch(self) -> int:
        return 10

    async def send(self, batch: list[PendingNotification]) -> SendResult:
        if not batch:
            return SendResult.ok([])

        individually = batch[:DIGEST_THRESHOLD]
        overflow = batch[DIGEST_THRESHOLD:]
        delivered: list[int] = []

        for notification in individually:
            result = await self._post(
                "sendMessage",
                self._verdict_payload(notification)
                if notification.is_verdict
                else self._listing_payload(
                    notification.item,
                    query_id=notification.query_id,
                    headline=notification.headline() if notification.is_price_drop else None,
                    enrichment=notification.enrichment,
                ),
                [notification.outbox_id],
            )
            if not result.delivered:
                remaining = [n.outbox_id for n in batch if n.outbox_id not in delivered]
                return SendResult(
                    delivered=delivered,
                    retry=[] if result.permanent_error else remaining,
                    retry_after_s=result.retry_after_s,
                    permanent_error=result.permanent_error,
                    error=result.error,
                )
            delivered.extend(result.delivered)

        if overflow:
            result = await self._post(
                "sendMessage",
                self._digest_payload(overflow),
                [n.outbox_id for n in overflow],
            )
            if not result.delivered:
                return SendResult(
                    delivered=delivered,
                    retry=[] if result.permanent_error else result.retry,
                    retry_after_s=result.retry_after_s,
                    permanent_error=result.permanent_error,
                    error=result.error,
                )
            delivered.extend(result.delivered)

        return SendResult.ok(delivered)

    async def send_status(self, message: str) -> None:
        await self._post(
            "sendMessage",
            self._base_payload() | {"text": html.escape(message)[:MAX_MESSAGE_CHARS]},
            [],
        )

    def _base_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self._chat_id, "parse_mode": "HTML"}
        if self._thread_id:
            payload["message_thread_id"] = self._thread_id
        return payload

    def _listing_payload(
        self,
        item: Item,
        *,
        query_id: int | None = None,
        headline: str | None = None,
        enrichment: Enrichment | None = None,
    ) -> dict[str, Any]:
        lines = [f"<b>{html.escape(item.title)}</b>", html.escape(item.price_line())]
        if headline:
            lines.insert(0, f"📉 <b>{html.escape(headline)}</b>")
        silent = enrichment is not None and enrichment.is_dull(self._silent_below)
        if enrichment is not None:
            self._weave_verdict(lines, item, enrichment, silent=silent)

        details = " · ".join(
            html.escape(part) for part in (item.brand, item.size, item.condition) if part
        )
        if details:
            lines.append(details)
        if item.seller_login:
            seller = html.escape(item.seller_login)
            if item.seller_rating is not None:
                seller += f" ({item.seller_rating:.0%})"
            lines.append(f"Seller: {seller}")
        if item.listed_at:
            lines.append(f"Listed {item.listed_at.strftime('%H:%M UTC')}")

        keyboard: list[list[dict[str, str]]] = [_link_row(item)]
        # A second row of actions the bot handles itself: the two things people most
        # often want to do from the alert without opening the dashboard.
        actions = inline_actions(query_id, item.seller_login)
        if actions:
            keyboard.append(actions)

        payload = self._base_payload() | {
            "text": "\n".join(lines)[:MAX_MESSAGE_CHARS],
            "reply_markup": {"inline_keyboard": keyboard},
        }
        if silent:
            # Still delivered, still in the chat, just no buzz for something the outside
            # brain rated as not worth one.
            payload["disable_notification"] = True
        if item.photo_url:
            payload["link_preview_options"] = {
                "url": item.photo_url,
                "prefer_large_media": True,
                "show_above_text": True,
            }
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        return payload

    def _verdict_payload(self, notification: PendingNotification) -> dict[str, Any]:
        """A follow-up for a verdict that arrived after the alert: short, and only sent
        when the deal is hot, so it earns its buzz."""
        item = notification.item
        payable = item.total_price if item.total_price is not None else item.price
        lines = [f"🔥 <b>Verdict is in: hot deal</b> · {html.escape(item.title)}"]
        if notification.enrichment is not None:
            summary, details = notification.enrichment.lines(payable, item.currency)
            if summary:
                lines.append(html.escape(summary))
            lines.extend(f"<i>{html.escape(detail)}</i>" for detail in details)
        return self._base_payload() | {
            "text": "\n".join(lines)[:MAX_MESSAGE_CHARS],
            "reply_markup": {"inline_keyboard": [[{"text": "Open listing", "url": item.url}]]},
            "link_preview_options": {"is_disabled": True},
        }

    def _weave_verdict(
        self, lines: list[str], item: Item, verdict: Enrichment, *, silent: bool
    ) -> None:
        payable = item.total_price if item.total_price is not None else item.price
        summary, details = verdict.lines(payable, item.currency)
        if summary:
            hot = verdict.is_hot(self._highlight_score)
            marker = "🔥 <b>HOT DEAL</b> · " if hot else ("💤 " if silent else "🤖 ")
            lines.insert(0, f"{marker}{html.escape(summary)}")
        lines.extend(f"<i>{html.escape(detail)}</i>" for detail in details)

    def _digest_payload(self, batch: list[PendingNotification]) -> dict[str, Any]:
        lines = [f"<b>{len(batch)} more matches</b>"]
        for index, notification in enumerate(batch, start=1):
            item = notification.item
            marker = "📉 " if notification.is_price_drop else ""
            lines.append(
                f'{index}. {marker}<a href="{html.escape(item.url, quote=True)}">'
                f"{html.escape(item.title[:80])}</a> — {html.escape(item.price_line())}"
            )
        return self._base_payload() | {
            "text": "\n".join(lines)[:MAX_MESSAGE_CHARS],
            "link_preview_options": {"is_disabled": True},
        }

    async def _post(
        self, method: str, payload: dict[str, Any], outbox_ids: list[int]
    ) -> SendResult:
        await self._bucket.acquire()
        try:
            response = await self._client.post(
                f"{API_ROOT}/bot{self._token}/{method}", json=payload
            )
        except httpx.HTTPError as exc:
            return SendResult.transient(outbox_ids, f"could not reach Telegram: {exc}")

        try:
            body = response.json()
        except ValueError:
            return SendResult.transient(outbox_ids, "Telegram sent a reply we could not read")

        if response.is_success and body.get("ok"):
            return SendResult.ok(outbox_ids)

        description = str(body.get("description", response.text))[:300]
        retry_after = body.get("parameters", {}).get("retry_after")

        if retry_after is not None:
            log.warning("telegram.rate_limited", retry_after_s=retry_after)
            return SendResult(
                delivered=[],
                retry=outbox_ids,
                retry_after_s=float(retry_after),
                error="rate limited by Telegram",
            )

        return _classify(description, response.status_code, outbox_ids)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# Telegram allows 64 bytes of callback data per button.
_CALLBACK_LIMIT = 64
CALLBACK_BLOCK_SELLER = "bs"
CALLBACK_PAUSE_SEARCH = "ps"


def _link_row(item: Item) -> list[dict[str, str]]:
    """Only links that resolve. Vinted's old deep links to the message screen and to
    checkout answer "page not found" since the site was rebuilt; both actions now live on
    the listing page itself, so one button covers them."""
    row = [{"text": "Open listing", "url": item.url}]
    if item.seller_url:
        row.append({"text": "Seller profile", "url": item.seller_url})
    return row


def inline_actions(query_id: int | None, seller_login: str | None) -> list[dict[str, str]]:
    """Callback buttons the bot in botctl answers. Empty when there is no search to act on."""
    if query_id is None:
        return []
    row: list[dict[str, str]] = []
    if seller_login:
        data = f"{CALLBACK_BLOCK_SELLER}:{query_id}:{seller_login}"
        if len(data.encode()) <= _CALLBACK_LIMIT:
            row.append({"text": "🚫 Skip seller", "callback_data": data})
    row.append({"text": "⏸ Pause search", "callback_data": f"{CALLBACK_PAUSE_SEARCH}:{query_id}"})
    return row


def _classify(description: str, status_code: int, outbox_ids: list[int]) -> SendResult:
    """Decide what a Telegram error means for the queue."""
    lowered = description.lower()
    if any(marker in lowered for marker in _PERMANENT_MARKERS):
        return SendResult.gone(f"Telegram says: {description}")

    if status_code == httpx.codes.BAD_REQUEST:
        # Malformed request; sending it again changes nothing.
        log.error("telegram.rejected_payload", description=description)
        return SendResult(delivered=[], retry=[], error=f"Telegram rejected it: {description}")

    return SendResult.transient(outbox_ids, f"Telegram error: {description}")
