"""One listing to the verdict flow: what it sends, what it accepts, and how it fails.

Two assertions carry this file. The first is that the request has the full `photo_urls` in
it — this is the one stage where paying for full-size photos is correct, and the cap that
makes that affordable lives in `judge_sweep()`, not here. The second is that the answer
comes back through `enrichment.EnrichmentIn` itself: if this file ever needs a verdict type
of its own, "a copy of the enrichment flow, unchanged" has stopped being true.

The rest mirrors `test_magic_triage.py` — every failure arrives as a `MappingError`, tagged
with the `kind` an operator greps for.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageTarget
from vinted_sniper.magic.verdict import VerdictClient
from vinted_sniper.vinted.models import Item

TARGET = TriageTarget(
    keywords=["torrentshell", "patagonia"],
    visual_signature="a lightweight shell jacket with a hood and a chest logo",
    labels={"brand": "Patagonia", "catalog": "Jackets & Coats"},
)

# What a copy of the enrichment flow answers with: the callback's own body, nothing around
# it. Every field is optional there, and a partial verdict beats none.
BARE_ANSWER = {
    "score": 78,
    "model": "Patagonia Torrentshell 3L",
    "retail_price": "180.00",
    "retail_source": "patagonia.com",
    "matches_query": True,
    "risk": "no interior label in the photos",
    "verdict": "a genuine 3L shell at less than half retail",
}

# What a flow written for this stage answers with: the same verdict, priced.
ENVELOPE_ANSWER = {
    "verdict": BARE_ANSWER,
    "usage": {"input_tokens": 14_600, "output_tokens": 240},
}


def item(item_id: int, **overrides: object) -> Item:
    fields: dict[str, object] = {
        "item_id": item_id,
        "tld": "sk",
        "title": f"Kurtka Patagonia {item_id}",
        "url": f"https://www.vinted.sk/items/{item_id}",
        "brand": "Patagonia",
        "size": "M",
        "condition": "Very good",
        "price": Decimal("55.00"),
        "total_price": Decimal("61.20"),
        "currency": "EUR",
        "photo_url": f"https://images.vinted.net/f800/{item_id}.jpeg",
        "thumb_url": f"https://images.vinted.net/310x430/{item_id}.jpeg",
        "photo_urls": (
            f"https://images.vinted.net/f800/{item_id}.jpeg",
            f"https://images.vinted.net/f800/{item_id}-b.jpeg",
        ),
        "seller_login": "hikergirl",
    }
    fields.update(overrides)
    return Item(**fields)  # type: ignore[arg-type]


ITEM = item(1)


class FakeFlow:
    """A stand-in n8n verdict flow: records what arrived, answers what it was given."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.calls = 0
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self.raises: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json=ENVELOPE_ANSWER)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def verdict(fake: FakeFlow, *, token: SecretStr | None = None) -> VerdictClient:
    return VerdictClient("https://n8n.test/webhook/verdict", token=token, client=fake.client())


# --- What goes out --------------------------------------------------------------------


async def test_one_candidate_is_exactly_one_request() -> None:
    """The cap in `judge_sweep()` counts requests, so one call must mean one call."""
    fake = FakeFlow()

    await verdict(fake).judge(ITEM, TARGET)

    assert fake.calls == 1


async def test_the_request_carries_every_full_size_photo() -> None:
    """The opposite of triage's assertion, and deliberately so: this stage looks properly."""
    fake = FakeFlow()

    await verdict(fake).judge(ITEM, TARGET)

    sent = json.loads(fake.requests[0].content)["items"][0]
    assert sent["photo_urls"] == [
        "https://images.vinted.net/f800/1.jpeg",
        "https://images.vinted.net/f800/1-b.jpeg",
    ]
    assert sent["photo_url"] == "https://images.vinted.net/f800/1.jpeg"


async def test_the_item_block_is_the_webhooks_shape_without_the_callback() -> None:
    """A sweep candidate has no `enrichment_url`: its answer comes back on the response.

    Sending one would point the flow at `POST /api/items/<id>/enrichment`, which writes
    `items` — the one table a sweep must never touch (MEM007).
    """
    fake = FakeFlow()

    await verdict(fake).judge(ITEM, TARGET)

    body = json.loads(fake.requests[0].content)
    assert "enrichment_url" not in fake.requests[0].content.decode()
    assert body["items"][0] == {
        "id": 1,
        "site": "vinted.sk",
        "title": "Kurtka Patagonia 1",
        "url": "https://www.vinted.sk/items/1",
        "brand": "Patagonia",
        "size": "M",
        "condition": "Very good",
        "price": "55.00",
        "total_price": "61.20",
        "currency": "EUR",
        "photo_url": "https://images.vinted.net/f800/1.jpeg",
        "photo_urls": [
            "https://images.vinted.net/f800/1.jpeg",
            "https://images.vinted.net/f800/1-b.jpeg",
        ],
        "seller": "hikergirl",
    }


async def test_the_request_carries_the_version_the_source_and_the_target() -> None:
    """`source` keeps sweep traffic separable from standing-watch traffic in n8n's history."""
    fake = FakeFlow()

    await verdict(fake).judge(ITEM, TARGET)

    body = json.loads(fake.requests[0].content)
    assert body["version"] == 1
    assert body["source"] == "sweep"
    assert body["target"] == {
        "keywords": ["torrentshell", "patagonia"],
        "visual_signature": "a lightweight shell jacket with a hood and a chest logo",
        "labels": {"brand": "Patagonia", "catalog": "Jackets & Coats"},
    }


async def test_a_configured_token_is_sent_as_a_bearer() -> None:
    fake = FakeFlow()

    await verdict(fake, token=SecretStr("s3cret")).judge(ITEM, TARGET)

    assert fake.requests[0].headers["Authorization"] == "Bearer s3cret"


async def test_no_token_means_no_authorization_header() -> None:
    fake = FakeFlow()

    await verdict(fake).judge(ITEM, TARGET)

    assert "authorization" not in fake.requests[0].headers


# --- What comes back ------------------------------------------------------------------


async def test_the_answer_parses_through_the_enrichment_type() -> None:
    """Not "a type shaped like EnrichmentIn" — the type itself, or the reuse is a fiction."""
    fake = FakeFlow()

    out = await verdict(fake).judge(ITEM, TARGET)

    assert isinstance(out.verdict, EnrichmentIn)
    assert out.verdict.score == 78
    assert out.verdict.model == "Patagonia Torrentshell 3L"
    assert out.verdict.retail_price == Decimal("180.00")
    assert out.verdict.matches_query is True
    assert out.verdict.verdict == "a genuine 3L shell at less than half retail"
    assert out.usage is not None
    assert out.usage.input_tokens == 14_600


async def test_a_bare_enrichment_body_with_no_usage_parses() -> None:
    """What an unmodified copy of the enrichment flow actually answers with.

    No `usage` is not an error: the sweep prices the call from the configured rates.
    """
    fake = FakeFlow(httpx.Response(200, json=BARE_ANSWER))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.usage is None
    assert out.verdict.score == 78
    assert out.verdict.verdict == "a genuine 3L shell at less than half retail"


async def test_a_bare_body_whose_verdict_is_a_string_is_not_mistaken_for_an_envelope() -> None:
    """`verdict` means two things across the two shapes; a dict is the only discriminator."""
    fake = FakeFlow(httpx.Response(200, json={"verdict": "worth a look", "score": 61}))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.verdict.verdict == "worth a look"
    assert out.verdict.score == 61


async def test_a_partial_verdict_is_accepted() -> None:
    """Every field on the callback is optional, and a partial answer beats none."""
    fake = FakeFlow(httpx.Response(200, json={"verdict": {"score": 40}}))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.verdict.score == 40
    assert out.verdict.model is None
    assert out.verdict.matches_query is None


async def test_a_reported_cost_is_carried_through() -> None:
    answer = {
        "verdict": BARE_ANSWER,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_eur": 0.031},
    }
    fake = FakeFlow(httpx.Response(200, json=answer))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.usage is not None
    assert out.usage.cost_eur == pytest.approx(0.031)


async def test_an_output_wrapper_is_unwrapped() -> None:
    fake = FakeFlow(httpx.Response(200, json={"output": ENVELOPE_ANSWER}))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.verdict.score == 78


async def test_a_result_wrapper_around_a_bare_body_is_unwrapped() -> None:
    fake = FakeFlow(httpx.Response(200, json={"result": BARE_ANSWER}))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.verdict.model == "Patagonia Torrentshell 3L"


async def test_an_unknown_field_does_not_break_the_parse() -> None:
    """The flow may start answering with a new field before this app knows about it."""
    answer = {
        "verdict": {**BARE_ANSWER, "colour": "blue"},
        "usage": {"model": "haiku"},
        "trace_id": "abc",
    }
    fake = FakeFlow(httpx.Response(200, json=answer))

    out = await verdict(fake).judge(ITEM, TARGET)

    assert out.verdict.score == 78


# --- How it fails ---------------------------------------------------------------------


async def test_a_score_out_of_range_is_refused() -> None:
    """`EnrichmentIn` bounds the score 0-100, and this stage does not get to relax that."""
    fake = FakeFlow(httpx.Response(200, json={"verdict": {"score": 900}}))

    with pytest.raises(MappingError, match="score"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_a_wrongly_typed_field_is_a_mapping_error_naming_it() -> None:
    fake = FakeFlow(httpx.Response(200, json={"verdict": {"score": "quite good"}}))

    with pytest.raises(MappingError, match="unusable shape"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_a_timeout_is_a_mapping_error_not_an_httpx_one() -> None:
    fake = FakeFlow()
    fake.raises = httpx.TimeoutException("read timed out")

    with pytest.raises(MappingError, match="did not answer in time"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_a_connection_failure_is_a_mapping_error() -> None:
    fake = FakeFlow()
    fake.raises = httpx.ConnectError("no route to host")

    with pytest.raises(MappingError, match="could not reach the verdict flow"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_a_500_is_a_mapping_error_naming_the_status() -> None:
    fake = FakeFlow(httpx.Response(500, text="boom"))

    with pytest.raises(MappingError, match="500"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_a_body_that_is_not_json_is_a_mapping_error() -> None:
    """A paused flow answers 200 with HTML. That must not surface as a JSON bug."""
    fake = FakeFlow(httpx.Response(200, text="<html>Workflow could not be started</html>"))

    with pytest.raises(MappingError, match="not JSON"):
        await verdict(fake).judge(ITEM, TARGET)


async def test_json_that_is_not_an_object_is_a_mapping_error() -> None:
    """n8n answering with a bare node array is the common shape of this mistake."""
    fake = FakeFlow(httpx.Response(200, json=[BARE_ANSWER]))

    with pytest.raises(MappingError, match="not a JSON object"):
        await verdict(fake).judge(ITEM, TARGET)
