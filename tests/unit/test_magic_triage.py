"""One batch to the photo check: what it sends, what it accepts, and how it fails.

The first assertion in this file is the one that matters most, and it is not about
behaviour at all. Sending full-size photos instead of thumbnails breaks nothing — the
sweep runs, the ranking is fine, the bill is roughly six times bigger and nobody notices
for a month. A cost regression with no runtime symptom is only ever caught by an assertion
on the outbound body, so that is what `test_the_batch_carries_thumbnails_and_no_full_photos`
is for.

The rest mirrors `test_magic_client.py`: every failure arrives as a `MappingError`, tagged
with the `kind` an operator greps for.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
from pydantic import SecretStr

from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import TriageTarget
from vinted_sniper.magic.triage import TriageClient
from vinted_sniper.vinted.models import Item

TARGET = TriageTarget(
    keywords=["torrentshell", "patagonia"],
    visual_signature="a lightweight shell jacket with a hood and a chest logo",
    labels={"brand": "Patagonia", "catalog": "Jackets & Coats"},
)

GOOD_ANSWER: dict[str, Any] = {
    "results": [
        {"id": 1, "matches_target": True, "confidence": 0.91, "reason": "the hood and cut match"},
        {"id": 2, "matches_target": False, "confidence": 0.2, "reason": "a fleece, not a shell"},
    ],
    "usage": {"input_tokens": 4200, "output_tokens": 130},
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
    }
    fields.update(overrides)
    return Item(**fields)  # type: ignore[arg-type]


BATCH = [item(1), item(2)]


class FakeFlow:
    """A stand-in n8n triage flow: records what arrived, answers with what it was given."""

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
        return httpx.Response(200, json=GOOD_ANSWER)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def triage(fake: FakeFlow, *, token: SecretStr | None = None) -> TriageClient:
    return TriageClient("https://n8n.test/webhook/triage", token=token, client=fake.client())


async def test_a_good_answer_becomes_a_triage_batch() -> None:
    fake = FakeFlow()

    batch = await triage(fake).judge(BATCH, TARGET)

    assert [result.id for result in batch.results] == [1, 2]
    assert batch.results[0].matches_target is True
    assert batch.results[0].confidence == pytest.approx(0.91)
    assert batch.results[1].matches_target is False
    assert batch.usage is not None
    assert batch.usage.input_tokens == 4200


async def test_one_batch_is_exactly_one_request() -> None:
    """R004's token economy is one call per batch. A retry loop would silently double it."""
    fake = FakeFlow()

    await triage(fake).judge(BATCH, TARGET)

    assert fake.calls == 1


async def test_the_batch_carries_thumbnails_and_no_full_photos() -> None:
    """The cost assertion. Sending full-size photos here works and costs ~6x — invisible
    at runtime, so this is the only place it is ever caught."""
    fake = FakeFlow()

    await triage(fake).judge(BATCH, TARGET)

    raw = fake.requests[0].content.decode()
    assert "photo_url" not in raw
    assert "photo_urls" not in raw
    body = json.loads(raw)
    assert len(body["items"]) == 2
    for sent in body["items"]:
        assert sent["thumb_url"].startswith("https://images.vinted.net/310x430/")


async def test_the_request_carries_the_version_the_source_and_the_target() -> None:
    fake = FakeFlow()

    await triage(fake).judge(BATCH, TARGET)

    body = json.loads(fake.requests[0].content)
    assert body["version"] == 1
    assert body["source"] == "sweep"
    assert body["target"] == {
        "keywords": ["torrentshell", "patagonia"],
        "visual_signature": "a lightweight shell jacket with a hood and a chest logo",
        "labels": {"brand": "Patagonia", "catalog": "Jackets & Coats"},
    }


async def test_each_item_carries_the_words_around_the_photo() -> None:
    fake = FakeFlow()

    await triage(fake).judge([item(7)], TARGET)

    sent = json.loads(fake.requests[0].content)["items"][0]
    assert sent == {
        "id": 7,
        "thumb_url": "https://images.vinted.net/310x430/7.jpeg",
        "title": "Kurtka Patagonia 7",
        "price": "55.00",
        "currency": "EUR",
        "brand": "Patagonia",
        "size": "M",
        "condition": "Very good",
    }


async def test_an_item_without_a_thumbnail_is_still_sent() -> None:
    """Dropping it here would remove a listing from the ranking for a reason nobody could
    see afterwards. The model judges it on the title and says so."""
    fake = FakeFlow()

    await triage(fake).judge([item(3, thumb_url=None, photo_url=None)], TARGET)

    sent = json.loads(fake.requests[0].content)["items"]
    assert len(sent) == 1
    assert sent[0]["id"] == 3
    assert sent[0]["thumb_url"] is None


async def test_a_configured_token_is_sent_as_a_bearer() -> None:
    fake = FakeFlow()

    await triage(fake, token=SecretStr("s3cret")).judge(BATCH, TARGET)

    assert fake.requests[0].headers["Authorization"] == "Bearer s3cret"


async def test_no_token_means_no_authorization_header() -> None:
    fake = FakeFlow()

    await triage(fake).judge(BATCH, TARGET)

    assert "authorization" not in fake.requests[0].headers


async def test_an_output_wrapper_is_unwrapped() -> None:
    fake = FakeFlow(httpx.Response(200, json={"output": GOOD_ANSWER}))

    batch = await triage(fake).judge(BATCH, TARGET)

    assert [result.id for result in batch.results] == [1, 2]


async def test_a_result_wrapper_is_unwrapped() -> None:
    fake = FakeFlow(httpx.Response(200, json={"result": GOOD_ANSWER}))

    batch = await triage(fake).judge(BATCH, TARGET)

    assert len(batch.results) == 2


async def test_usage_absent_parses_as_none() -> None:
    """A flow that reports no cost is not an error — T06 prices it from the configured
    rates instead."""
    fake = FakeFlow(httpx.Response(200, json={"results": GOOD_ANSWER["results"]}))

    batch = await triage(fake).judge(BATCH, TARGET)

    assert batch.usage is None


async def test_a_reported_cost_is_carried_through() -> None:
    answer = {**GOOD_ANSWER, "usage": {"input_tokens": 10, "output_tokens": 5, "cost_eur": 0.0042}}
    fake = FakeFlow(httpx.Response(200, json=answer))

    batch = await triage(fake).judge(BATCH, TARGET)

    assert batch.usage is not None
    assert batch.usage.cost_eur == pytest.approx(0.0042)


async def test_an_unknown_field_does_not_break_the_parse() -> None:
    """The flow may start answering with a new field before this app knows about it."""
    answer = {
        "results": [{"id": 1, "matches_target": True, "confidence": 0.5, "colour": "blue"}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "model": "haiku"},
        "trace_id": "abc",
    }
    fake = FakeFlow(httpx.Response(200, json=answer))

    batch = await triage(fake).judge(BATCH, TARGET)

    assert batch.results[0].id == 1


async def test_a_confidence_above_one_is_refused_not_ranked_on() -> None:
    """A flow answering 87 when it meant 0.87 would dominate every real match."""
    answer = {"results": [{"id": 1, "matches_target": True, "confidence": 87}]}
    fake = FakeFlow(httpx.Response(200, json=answer))

    with pytest.raises(MappingError, match="confidence"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_negative_confidence_is_refused() -> None:
    answer = {"results": [{"id": 1, "matches_target": True, "confidence": -0.5}]}
    fake = FakeFlow(httpx.Response(200, json=answer))

    with pytest.raises(MappingError, match="confidence"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_timeout_is_a_mapping_error_not_an_httpx_one() -> None:
    fake = FakeFlow()
    fake.raises = httpx.TimeoutException("read timed out")

    with pytest.raises(MappingError, match="did not answer in time"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_connection_failure_is_a_mapping_error() -> None:
    fake = FakeFlow()
    fake.raises = httpx.ConnectError("no route to host")

    with pytest.raises(MappingError, match="could not reach the photo check"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_500_is_a_mapping_error_naming_the_status() -> None:
    fake = FakeFlow(httpx.Response(500, text="boom"))

    with pytest.raises(MappingError, match="500"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_body_that_is_not_json_is_a_mapping_error() -> None:
    """A paused flow answers 200 with HTML. That must not surface as a JSON bug."""
    fake = FakeFlow(httpx.Response(200, text="<html>Workflow could not be started</html>"))

    with pytest.raises(MappingError, match="not JSON"):
        await triage(fake).judge(BATCH, TARGET)


async def test_json_that_is_not_an_object_is_a_mapping_error() -> None:
    """n8n answering with a bare node array is the common shape of this mistake."""
    fake = FakeFlow(httpx.Response(200, json=[GOOD_ANSWER]))

    with pytest.raises(MappingError, match="not a JSON object"):
        await triage(fake).judge(BATCH, TARGET)


async def test_a_schema_violation_names_the_field_that_was_wrong() -> None:
    bad = {"results": [{"id": "one", "matches_target": True, "confidence": 0.5}]}
    fake = FakeFlow(httpx.Response(200, json=bad))

    with pytest.raises(MappingError, match=r"results\.0\.id") as caught:
        await triage(fake).judge(BATCH, TARGET)

    assert "unusable shape" in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "response", "raises"),
    [
        ("timeout", None, httpx.TimeoutException("slow")),
        ("unreachable", None, httpx.ConnectError("no route")),
        ("status", httpx.Response(503, text="down"), None),
        ("not_json", httpx.Response(200, text="<html/>"), None),
        ("not_object", httpx.Response(200, json=["nope"]), None),
        ("schema", httpx.Response(200, json={"results": [{"id": 1}]}), None),
    ],
)
async def test_every_failure_arm_logs_its_own_kind(
    kind: str, response: httpx.Response | None, raises: Exception | None
) -> None:
    """`kind` is what a dashboard groups on, so each arm has to carry its own."""
    fake = FakeFlow(*([response] if response is not None else []))
    fake.raises = raises

    with structlog.testing.capture_logs() as entries, pytest.raises(MappingError):
        await triage(fake).judge(BATCH, TARGET)

    failures = [entry for entry in entries if entry["event"] == "magic.triage_failed"]
    assert [entry["kind"] for entry in failures] == [kind]


async def test_a_successful_batch_logs_what_it_sent_and_what_came_back() -> None:
    """A flow answering with the wrong count is visible per batch, not only at the end."""
    fake = FakeFlow(httpx.Response(200, json={"results": [GOOD_ANSWER["results"][0]]}))

    with structlog.testing.capture_logs() as entries:
        await triage(fake).judge(BATCH, TARGET)

    triaged = [entry for entry in entries if entry["event"] == "magic.triaged"]
    assert triaged == [{"event": "magic.triaged", "log_level": "info", "sent": 2, "returned": 1}]


async def test_an_injected_client_is_left_open() -> None:
    fake = FakeFlow()
    client = fake.client()
    subject = TriageClient("https://n8n.test/triage", client=client)

    await subject.aclose()

    assert not client.is_closed


async def test_an_owned_client_is_closed() -> None:
    subject = TriageClient("https://n8n.test/triage")

    await subject.aclose()

    assert subject._client.is_closed
