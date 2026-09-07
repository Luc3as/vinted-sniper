"""The stage that judges a listing by what it looks like, not by what it was called.

The milestone exists because of one number: of 89 real matches in the reference sweep, 0
named the model in their title (R003/R005). A title score can only rank what a seller
happened to type, so the funnel hands its survivors here and the flow looks at the photo.

Structurally this is `client.py` again — one attempt, no retries, every way of failing
arriving as a single `MappingError` — and deliberately so. Both talk to the same n8n, both
are paid per call, and an operator debugging one should not have to learn a second set of
rules. What differs is only that this one goes out in batches and reports what it spent.

**Thumbnails only.** Every item in the request carries `thumb_url` and nothing else
image-shaped. That is the whole cost model (R004): a ~310x430 thumbnail is roughly 180
image tokens where a full-size photo is about six times that, and a sweep sends ~150 of
them. Sending `photo_url` here would still work — it would just quietly cost six times as
much, which is why `tests/unit/test_magic_triage.py` asserts on the outbound body rather
than trusting a comment. Full-size photos are correct in `verdict.py`, where there are
two or three of them.

No prompt text lives here (D007). This module owns the JSON contract and the batch it
sends; the flow owns the model and the wording.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NoReturn

import httpx
from pydantic import SecretStr, ValidationError

from vinted_sniper.log import get_logger
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageBatch, TriageTarget
from vinted_sniper.vinted.models import Item

log = get_logger(__name__)

REQUEST_VERSION = 1

# n8n habitually returns a node's output wrapped under a single key. Unwrapping one level
# when the whole payload is exactly one of these means a flow rewired to answer
# `{"output": {...}}` instead of a bare object does not need an app redeploy.
_WRAPPER_KEYS = ("output", "result")


class TriageClient:
    """Asks an n8n flow which of these thumbnails is the thing you described."""

    def __init__(
        self,
        url: str,
        *,
        token: SecretStr | None = None,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url
        self._token = token
        # Injectable for tests, owned otherwise — the same arrangement as MapperClient, so
        # a MockTransport can stand in for the flow without the client leaking a real one.
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def judge(self, items: Sequence[Item], target: TriageTarget) -> TriageBatch:
        """POST one batch of listings once and return the flow's verdict on each.

        The caller does the batching: this sends exactly one request for whatever it is
        given, so `judge_sweep()` decides the batch size and this decides nothing about
        cost except what goes into each item.

        An item whose `thumb_url` is None is still sent. The model judges it on the title
        alone and says so in `reason`; dropping it here would silently remove a listing
        from the ranking for a reason nobody could see afterwards.

        Raises `MappingError` and nothing else — the same six failure arms as the mapper,
        for the same reason: the caller is a sweep, and a sweep degrades to `partial`
        rather than learning what an `httpx` exception type means.
        """
        headers = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"

        body = {
            "version": REQUEST_VERSION,
            # Keeps sweep traffic separable from standing-watch traffic in the operator's
            # n8n execution history, at no cost.
            "source": "sweep",
            "target": target.model_dump(mode="json"),
            "items": [_item_json(item) for item in items],
        }

        try:
            response = await self._client.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            _fail("timeout", f"the photo check did not answer in time: {exc}")
        except httpx.HTTPError as exc:
            _fail("unreachable", f"could not reach the photo check: {exc}")

        if not response.is_success:
            _fail("status", f"the photo check answered {response.status_code}")

        try:
            payload: Any = response.json()
        except ValueError:
            _fail("not_json", "the photo check answered with something that is not JSON")

        if not isinstance(payload, dict):
            shape = type(payload).__name__
            _fail("not_object", f"the photo check answered with a {shape}, not a JSON object")

        payload = _unwrap(payload)

        try:
            batch = TriageBatch.model_validate(payload)
        except ValidationError as exc:
            _fail("schema", f"the photo check answered with an unusable shape: {_first_error(exc)}")

        # Both counts, every batch. A flow that answers with fewer results than it was sent
        # is not an error here — the orchestrator reconciles by id — but it is the kind of
        # drift that is invisible until somebody asks why a sweep ranked nothing.
        log.info("magic.triaged", sent=len(items), returned=len(batch.results))
        return batch

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this instance made it."""
        if self._owns_client:
            await self._client.aclose()


def _item_json(item: Item) -> dict[str, Any]:
    """One listing as the photo check sees it: a thumbnail and the words around it.

    `thumb_url` is the only image key, on purpose — see the module docstring. Prices go out
    as strings, matching `deliver/webhook.py::_item_json()`, so no consumer has to think
    about float rounding on money.
    """
    return {
        "id": item.item_id,
        "thumb_url": item.thumb_url,
        "title": item.title,
        "price": str(item.price) if item.price is not None else None,
        "currency": item.currency,
        "brand": item.brand,
        "size": item.size,
        "condition": item.condition,
    }


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the inner object when the payload is only a single known wrapper key."""
    if len(payload) == 1:
        for key in _WRAPPER_KEYS:
            inner = payload.get(key)
            if isinstance(inner, dict):
                return inner
    return payload


def _first_error(exc: ValidationError) -> str:
    """The first pydantic complaint as `field: message`, so an operator can fix the flow."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    field = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
    return f"{field}: {first.get('msg', 'invalid')}"


def _fail(kind: str, message: str) -> NoReturn:
    """Log which way the batch failed, then raise. `kind` is what a dashboard groups on."""
    log.warning("magic.triage_failed", kind=kind, reason=message)
    raise MappingError(message)
