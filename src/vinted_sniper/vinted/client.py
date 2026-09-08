"""Fetching a search's results, and a seller's reputation when the results leave it out.

The care here goes into reading the response: a 200 from Vinted
is not a promise that the body is a catalog, and the tools that assumed it was are the ones
whose issue trackers fill up with tracebacks.
"""

from __future__ import annotations

import random
import time
from http import HTTPStatus
from typing import Any, Final

from vinted_sniper.log import get_logger
from vinted_sniper.vinted import headers as hdr
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.errors import (
    AuthExpiredError,
    BlockedError,
    MalformedResponseError,
    NetworkError,
    RateLimitedError,
    VintedError,
)
from vinted_sniper.vinted.models import Item, ParseError, parse_item
from vinted_sniper.vinted.pacing import RequestBudget
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.transport import Response, Transport, TransportError

log = get_logger(__name__)

# Vinted's own frontend sends a timestamp slightly behind the wall clock. Matching that,
# jitter included, is free and keeps our requests from looking machine-timed.
_TIME_SKEW_RANGE_S = (0, 180)

# Vinted's code for "your token is no longer valid", returned inside a 200 body as well
# as with a 401.
_INVALID_TOKEN_CODE: Final = 100

# The site caps this well below what you might ask for; 96 is what its own pages request.
PER_PAGE = 96

# A seller's numbers move slowly and one seller lists many items, so a fetched reputation
# is kept for a while rather than bought again for every listing. A failed read is
# remembered too, more briefly — reputation is best-effort, and retrying a broken profile
# read for every listing in the same batch would spend the site budget on nothing.
_SELLER_TTL_S = 6 * 3600
_SELLER_FAILURE_TTL_S = 600
_SELLER_CACHE_MAX = 512


class VintedClient:
    """Reads search results from one or more Vinted country sites."""

    def __init__(
        self,
        transport: Transport | None,
        sessions: SessionManager,
        *,
        keep_raw: bool = False,
        rng: random.Random | None = None,
        budget: RequestBudget | None = None,
    ) -> None:
        self._transport = transport
        self._sessions = sessions
        self._keep_raw = keep_raw
        self._rng = rng or random.Random()
        self._budget = budget
        # (tld, user_id) -> (expires_at, rating, review count). Insertion-ordered, so
        # trimming from the front drops the oldest entries.
        self._sellers: dict[tuple[str, int], tuple[float, float | None, int | None]] = {}

    async def search(self, tld: str, params: dict[str, str]) -> list[Item]:
        """Return the current first page of a search, newest first.

        Raises the error types in `errors.py`; the poller decides what each one means for
        the session and the schedule.
        """
        session = await self._sessions.get(tld)
        transport = self._transport or self._sessions.transport_for(session)
        query = dict(params)
        query["per_page"] = str(PER_PAGE)
        query["time"] = str(int(time.time()) - self._rng.randint(*_TIME_SKEW_RANGE_S))

        if self._budget is not None:
            waited = await self._budget.acquire(tld)
            if waited > 1.0:
                log.debug("catalog.paced", tld=tld, waited_s=round(waited, 1))

        try:
            response = await transport.get(
                urls.catalog_endpoint(tld),
                headers=hdr.api_headers(tld, session.identity),
                cookies=session.cookie_header,
                params=query,
            )
        except TransportError as exc:
            raise NetworkError(str(exc)) from exc

        raise_for_status(response, tld)
        await self._sessions.note_request(session)
        await self._sessions.merge_cookies(session, response.cookies)

        return _parse_catalog(response, tld, keep_raw=self._keep_raw)

    async def seller_reputation(self, tld: str, user_id: int) -> tuple[float | None, int | None]:
        """A seller's rating (0..1) and review count, read off their public profile.

        The catalog response used to carry both on every listing and quietly stopped, so
        an agent that cannot see 2351 reviews calls an established seller "new". The few
        listings that survive the filters buy one profile read each, cached, and paced by
        the same per-site budget as the searches.
        """
        key = (tld, user_id)
        cached = self._sellers.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1], cached[2]

        try:
            rating, reviews = await self._fetch_seller(tld, user_id)
        except VintedError:
            # The whole batch shares this cache: one broken profile read must not turn
            # into a paced retry for every listing of the same seller behind it.
            self._remember_seller(key, time.monotonic() + _SELLER_FAILURE_TTL_S, None, None)
            raise
        self._remember_seller(key, time.monotonic() + _SELLER_TTL_S, rating, reviews)
        return rating, reviews

    async def _fetch_seller(self, tld: str, user_id: int) -> tuple[float | None, int | None]:
        session = await self._sessions.get(tld)
        transport = self._transport or self._sessions.transport_for(session)
        if self._budget is not None:
            await self._budget.acquire(tld)

        try:
            response = await transport.get(
                urls.user_endpoint(tld, user_id),
                headers=hdr.api_headers(tld, session.identity),
                cookies=session.cookie_header,
                params={},
            )
        except TransportError as exc:
            raise NetworkError(str(exc)) from exc

        raise_for_status(response, tld)
        await self._sessions.note_request(session)
        await self._sessions.merge_cookies(session, response.cookies)
        return _parse_user(response, tld)

    def _remember_seller(
        self, key: tuple[str, int], expires_at: float, rating: float | None, reviews: int | None
    ) -> None:
        if len(self._sellers) >= _SELLER_CACHE_MAX:
            for stale in list(self._sellers)[: _SELLER_CACHE_MAX // 2]:
                del self._sellers[stale]
        self._sellers[key] = (expires_at, rating, reviews)


def raise_for_status(response: Response, tld: str) -> None:
    """Turn an HTTP status into the error type that says what to do about it."""
    status = response.status_code

    if status == HTTPStatus.OK:
        return

    if status == HTTPStatus.UNAUTHORIZED:
        raise AuthExpiredError(f"vinted.{tld} rejected the session token")

    if status in (HTTPStatus.FORBIDDEN, HTTPStatus.PROXY_AUTHENTICATION_REQUIRED):
        raise BlockedError(f"vinted.{tld} refused the request with {status}")

    if status == HTTPStatus.TOO_MANY_REQUESTS:
        raise RateLimitedError(_retry_after(response))

    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise NetworkError(f"vinted.{tld} returned {status}")

    raise NetworkError(f"vinted.{tld} returned an unexpected {status}")


def _retry_after(response: Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_catalog(response: Response, tld: str, *, keep_raw: bool) -> list[Item]:
    """Read a catalog response, refusing anything that is not one."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        # Most often this is an anti-bot interstitial served with a 200.
        preview = response.text[:200].replace("\n", " ")
        raise MalformedResponseError(f"vinted.{tld} did not return JSON: {preview!r}") from exc

    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"vinted.{tld} returned {type(payload).__name__}, expected an object"
        )

    if (message := payload.get("message")) and "items" not in payload:
        if payload.get("code") == _INVALID_TOKEN_CODE:
            raise AuthExpiredError(f"vinted.{tld} says: {message}")
        raise MalformedResponseError(f"vinted.{tld} says: {message}")

    raw_items = payload.get("items")
    if raw_items is None:
        raise MalformedResponseError(
            f"vinted.{tld} returned no items field; keys were {sorted(payload)[:8]}"
        )
    if not isinstance(raw_items, list):
        raise MalformedResponseError(
            f"vinted.{tld} returned items as {type(raw_items).__name__}, expected a list"
        )

    items: list[Item] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(parse_item(entry, tld, keep_raw=keep_raw))
        except ParseError as exc:
            # One malformed listing should not cost you the other ninety-five.
            log.warning("item.unparsed", tld=tld, error=str(exc))
    return items


def _parse_user(response: Response, tld: str) -> tuple[float | None, int | None]:
    """Read the reputation numbers out of a user response, refusing anything else."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        preview = response.text[:200].replace("\n", " ")
        raise MalformedResponseError(
            f"vinted.{tld} did not return JSON for a user: {preview!r}"
        ) from exc

    user = payload.get("user") if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        raise MalformedResponseError(f"vinted.{tld} returned no user object")

    rating = user.get("feedback_reputation")
    reviews = user.get("feedback_count")
    return (
        float(rating) if isinstance(rating, int | float) else None,
        reviews if isinstance(reviews, int) else None,
    )
