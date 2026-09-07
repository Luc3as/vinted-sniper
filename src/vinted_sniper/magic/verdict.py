"""The expensive opinion, on the two or three listings that earned one.

Triage looks at every survivor's thumbnail and answers one bit: is this the thing. This
stage looks at a listing properly — every full-size photo, the price, the seller — and
answers what the enrichment flow already answers for a standing watch: a score, the model
it thinks it is, a retail price, a risk note, one line for a human.

That "already answers" is the whole design (D009). The flow's prompt, its model and its
output contract do not move: this posts the same item shape `deliver/webhook.py::_item_json()`
posts, minus the `enrichment_url` callback, and parses the answer with `EnrichmentIn` —
imported, not redeclared. The only difference is who receives the answer. A standing
watch's verdict is written back through the callback into `items`; a sweep's is written by
the caller into `sweep_candidates`, because a sweep candidate has no row in `items` and
must never gain one (D005/MEM007). `Repo.store_enrichment()` is therefore not merely
wrong here, it is *silently* wrong — it is an `UPDATE items ... WHERE item_id = ?` that
returns `False` and writes nothing — which is why the isolation guard in
`tests/integration/test_sweep_isolation.py` refuses to let this module name it.

Full-size photos are correct here and only here. Triage sends ~150 thumbnails at roughly
180 image tokens each; this sends two or three listings' worth of `photo_urls` at roughly
six times that per photo, and the reason it is affordable is that `judge_sweep()` caps the
count *before* the calls are made, not in a prompt.

Structurally this is `triage.py` again — injectable client, one attempt, no retries, every
way of failing arriving as a single `MappingError` — for the same reason: both talk to the
same n8n and an operator debugging one should not have to learn a second set of rules.
"""

from __future__ import annotations

from typing import Any, NoReturn

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.log import get_logger
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageTarget, Usage
from vinted_sniper.vinted.models import Item

log = get_logger(__name__)

REQUEST_VERSION = 1

# Same one-level unwrap as the mapper and triage: a flow rewired to answer `{"output": {...}}`
# instead of a bare object does not need an app redeploy.
_WRAPPER_KEYS = ("output", "result")


class VerdictOut(BaseModel):
    """One full opinion, plus what it cost if the flow said.

    `verdict` is `EnrichmentIn` verbatim — the same type the standing watch's callback
    accepts — which is what makes "the same flow, unchanged" honest rather than a comment.
    A copy of the enrichment flow answers with a bare `EnrichmentIn`-shaped body and no
    `usage` at all; `VerdictClient.judge()` accepts that shape too and prices the call from
    the configured rates instead.
    """

    model_config = ConfigDict(extra="ignore")

    verdict: EnrichmentIn
    usage: Usage | None = None


class VerdictClient:
    """Asks a copy of the enrichment flow what it makes of one listing."""

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
        # Injectable for tests, owned otherwise — the same arrangement as TriageClient.
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def judge(self, item: Item, target: TriageTarget) -> VerdictOut:
        """POST one listing once and return the flow's opinion of it.

        One candidate per request, deliberately: this is the stage that is billed by the
        photo, the answers are per-listing, and a batch that half-failed would leave the
        caller unable to say which listings it had actually paid for.

        Raises `MappingError` and nothing else, so the caller degrades a run to `partial`
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
            # A one-element list rather than a bare object: the enrichment flow already
            # iterates `items`, and matching that shape is the point of reusing it.
            "items": [_item_json(item)],
        }

        try:
            response = await self._client.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            _fail("timeout", f"the verdict flow did not answer in time: {exc}")
        except httpx.HTTPError as exc:
            _fail("unreachable", f"could not reach the verdict flow: {exc}")

        if not response.is_success:
            _fail("status", f"the verdict flow answered {response.status_code}")

        try:
            payload: Any = response.json()
        except ValueError:
            _fail("not_json", "the verdict flow answered with something that is not JSON")

        if not isinstance(payload, dict):
            shape = type(payload).__name__
            _fail("not_object", f"the verdict flow answered with a {shape}, not a JSON object")

        out = _parse(_unwrap(payload))
        log.info(
            "magic.verdict",
            item_id=item.item_id,
            score=out.verdict.score,
            matches_query=out.verdict.matches_query,
        )
        return out

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this instance made it."""
        if self._owns_client:
            await self._client.aclose()


def _item_json(item: Item) -> dict[str, Any]:
    """One listing as the enrichment flow already expects to see it.

    The same keys `deliver/webhook.py::_item_json()` sends, minus `enrichment_url` — a
    sweep candidate has no callback, because its answer comes back on this response and is
    written to `sweep_candidates` — and minus the notification-only fields (`event`,
    `market_percentile`) that only exist for a listing a standing watch alerted on.

    `photo_urls` goes out in full. This is the one stage where that is the right cost.
    """
    return {
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
        "seller": item.seller_login,
    }


def _parse(payload: dict[str, Any]) -> VerdictOut:
    """Accept both shapes a flow can honestly answer with.

    `{"verdict": {...}, "usage": {...}}` is what a flow written for this stage returns. A
    plain copy of the enrichment flow returns the `EnrichmentIn` body itself, where
    `verdict` — if present at all — is the one-line string for a human rather than a nested
    object. That is the discriminator: a dict under `verdict` means the envelope, anything
    else means the answer is the whole body.
    """
    try:
        if isinstance(payload.get("verdict"), dict):
            return VerdictOut.model_validate(payload)
        return VerdictOut(
            verdict=EnrichmentIn.model_validate(payload),
            usage=_usage(payload.get("usage")),
        )
    except ValidationError as exc:
        _fail("schema", f"the verdict flow answered with an unusable shape: {_first_error(exc)}")


def _usage(raw: Any) -> Usage | None:
    """A usage block when the flow reported one, `None` when it did not. Never an error."""
    return Usage.model_validate(raw) if isinstance(raw, dict) else None


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
    """Log which way the call failed, then raise. `kind` is what a dashboard groups on."""
    log.warning("magic.verdict_failed", kind=kind, reason=message)
    raise MappingError(message)
