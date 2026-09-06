"""A plain JSON POST to a URL of your choosing.

The cheapest channel to support and the one that covers everything we did not build: n8n,
Home Assistant, Slack via an adapter, a shell script behind a tiny server. The payload is
documented in docs/configuration.md and treated as a contract — changing it breaks other
people's automations, so it changes only with a version bump.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from vinted_sniper.db.repo import PendingNotification
from vinted_sniper.deliver.base import SendResult, require
from vinted_sniper.deliver.ratelimit import TokenBucket
from vinted_sniper.log import get_logger

log = get_logger(__name__)

PAYLOAD_VERSION = 1


class WebhookSender:
    """Posts listings as JSON to an arbitrary endpoint."""

    kind = "webhook"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
        bucket: TokenBucket | None = None,
        callback_base: str | None = None,
        context: Callable[[PendingNotification], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._url = require(config, "url", self.kind)
        self._headers = config.get("headers") or {}
        self._callback_base = callback_base.rstrip("/") if callback_base else None
        # Extra facts an agent on the other end can lean on: where the price sits in
        # this search's market, retail prices already found, the buyer's past verdicts.
        self._context = context
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._bucket = bucket or TokenBucket(2.0, capacity=4)

    @property
    def max_batch(self) -> int:
        return 20

    async def send(self, batch: list[PendingNotification]) -> SendResult:
        if not batch:
            return SendResult.ok([])
        outbox_ids = [n.outbox_id for n in batch]
        items: list[dict[str, Any]] = []
        for notification in batch:
            entry = _item_json(notification, self._callback_base)
            if self._context is not None:
                try:
                    entry.update(await self._context(notification))
                except Exception as exc:  # noqa: BLE001 - a bonus, never a reason not to send
                    log.warning("webhook.context_failed", error=str(exc))
            items.append(entry)
        payload = {
            "version": PAYLOAD_VERSION,
            "search": batch[0].query_name,
            "search_id": batch[0].query_id,
            "items": items,
        }
        await self._bucket.acquire()
        try:
            response = await self._client.post(self._url, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            return SendResult.transient(outbox_ids, f"could not reach the webhook: {exc}")

        if response.is_success:
            return SendResult.ok(outbox_ids)
        if response.status_code in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
            return SendResult.gone(f"the endpoint answered {response.status_code}")
        return SendResult.transient(outbox_ids, f"the endpoint answered {response.status_code}")

    async def send_status(self, message: str) -> None:
        await self._bucket.acquire()
        try:
            await self._client.post(
                self._url,
                json={"version": PAYLOAD_VERSION, "status": message},
                headers=self._headers,
            )
        except httpx.HTTPError:
            return

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _minutes_since(ts: int | None) -> int | None:
    if ts is None:
        return None
    return max(0, int((time.time() - ts) / 60))


def _per_hour(count: int, since_ts: int | None) -> float | None:
    if since_ts is None:
        return None
    hours = max((time.time() - since_ts) / 3600, 0.25)  # a brand-new listing is not infinite
    return round(count / hours, 2)


def _item_json(notification: PendingNotification, callback_base: str | None) -> dict[str, Any]:
    item = notification.item
    return {
        # Additive: where an agent may post what it concluded about this listing (see
        # docs/enrichment.md). Absent when the dashboard is off.
        "enrichment_url": (
            f"{callback_base}/api/items/{item.item_id}/enrichment" if callback_base else None
        ),
        # Additive since version 1: "new" or "price_drop", and for a drop the total price
        # it fell from. Consumers that ignore unknown keys see no change.
        "event": notification.kind,
        "previous_total_price": (
            str(notification.previous_price) if notification.previous_price is not None else None
        ),
        "id": item.item_id,
        "site": f"vinted.{item.tld}",
        "title": item.title,
        "url": item.url,
        "brand": item.brand,
        "size": item.size,
        "condition": item.condition,
        "price": str(item.price) if item.price is not None else None,
        "total_price": str(item.total_price) if item.total_price is not None else None,
        "currency": item.currency,
        "photo_url": item.photo_url,
        "photo_urls": list(item.photo_urls),
        "listed_at": item.listed_at.isoformat() if item.listed_at else None,
        "seller": item.seller_login,
        "seller_rating": item.seller_rating,
        "seller_reviews": item.seller_feedback_count,
        # Demand: how many people have hearted it, and how fast that is happening.
        "favourites": item.favourite_count,
        "views": item.view_count,
        "listed_minutes_ago": _minutes_since(item.photo_ts),
        "favourites_per_hour": _per_hour(item.favourite_count, item.photo_ts),
        # Kept for consumers written against version 1. Both used to be Vinted deep links
        # that no longer resolve; they now point at the listing page, where the buttons are.
        "links": {
            "message_seller": item.message_url,
            "buy": item.buy_url,
            "seller": item.seller_url,
        },
    }
