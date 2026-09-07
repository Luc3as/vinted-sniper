"""The one outbound call that turns a sentence into structured search terms.

Everywhere else this app talks HTTP it either sends and forgets (`deliver/webhook.py`) or
sits still and waits to be called (`enrichment.py`). This is the first place it POSTs and
then *depends on the answer*, which makes the failure surface bigger than a webhook's: the
flow can be down, slow, answer with HTML from a proxy, or answer with JSON in a shape this
app cannot use. All four end the same way, as a `MappingError` in plain words, because the
caller — an endpoint answering a person who typed a sentence — has nothing useful to do
with an `httpx` exception type.

One request, no retries. A mapping is interactive: somebody is waiting, and every attempt
is a language-model call somebody pays for. A silent second try doubles both the latency
and the bill for the same likely-identical failure, so a failed mapping comes straight back
and the person can press the button again if they want to spend that.

The taxonomy check lives in `validate.py`, not here. A network failure and an id the model
invented are different problems with different fixes, and keeping them in different modules
is what stops them being reported as the same thing.
"""

from __future__ import annotations

from typing import Any, NoReturn

import httpx
from pydantic import SecretStr, ValidationError

from vinted_sniper.log import get_logger
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import MappedQuery

log = get_logger(__name__)

REQUEST_VERSION = 1

# n8n habitually returns a node's output wrapped under a single key. Unwrapping one level
# when the whole payload is exactly one of these means a flow rewired to answer
# `{"output": {...}}` instead of a bare object does not need an app redeploy.
_WRAPPER_KEYS = ("output", "result")


class MapperClient:
    """Asks an n8n flow to turn free text into search terms, and parses what comes back."""

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
        # Injectable for tests, owned otherwise — the same arrangement as WebhookSender, so
        # a MockTransport can stand in for the flow without the client leaking a real one.
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def map_query(self, text: str, tld: str) -> MappedQuery:
        """POST `text` to the mapper once and return the answer as a `MappedQuery`.

        The request is `{"version": 1, "text": ..., "tld": ...}` with a bearer header when a
        token is configured — the same credential shape this app's own API accepts, so the
        two directions are symmetrical and one secret covers both.

        A bare JSON object is expected back. A payload that is exactly `{"output": {...}}`
        or `{"result": {...}}` is unwrapped one level first, since n8n commonly nests a
        node's output under a single key.

        Raises `MappingError` and nothing else: unreachable, slow, non-2xx, not JSON, not an
        object, or an object `MappedQuery` refuses all arrive here as one readable failure.
        Ids in the returned query are *not* checked against Vinted — that is `validate()`.
        """
        headers = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"

        try:
            response = await self._client.post(
                self._url,
                json={"version": REQUEST_VERSION, "text": text, "tld": tld},
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            _fail("timeout", f"the mapper did not answer in time: {exc}")
        except httpx.HTTPError as exc:
            _fail("unreachable", f"could not reach the mapper: {exc}")

        if not response.is_success:
            _fail("status", f"the mapper answered {response.status_code}")

        try:
            payload: Any = response.json()
        except ValueError:
            _fail("not_json", "the mapper answered with something that is not JSON")

        if not isinstance(payload, dict):
            shape = type(payload).__name__
            _fail("not_object", f"the mapper answered with a {shape}, not a JSON object")

        payload = _unwrap(payload)

        try:
            mapped = MappedQuery.model_validate(payload)
        except ValidationError as exc:
            _fail("schema", f"the mapper answered with an unusable shape: {_first_error(exc)}")

        # Named for the step it is, not for the outcome: the flow answered with a shape
        # this app can use. Whether the ids in it are real is `validate()`'s answer, and
        # the endpoint logs that one as `magic.mapped`.
        log.info(
            "magic.flow_answered",
            tld=tld,
            catalog=mapped.catalog.id if mapped.catalog else None,
            brand=mapped.brand.id if mapped.brand else None,
            sizes=[size.id for size in mapped.sizes],
            keywords=len(mapped.keywords),
        )
        return mapped

    async def aclose(self) -> None:
        """Close the HTTP client, but only if this instance made it."""
        if self._owns_client:
            await self._client.aclose()


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
    """Log which way the mapping failed, then raise. `kind` is what a dashboard groups on."""
    log.warning("magic.map_failed", kind=kind, reason=message)
    raise MappingError(message)
