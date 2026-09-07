"""The one call to the mapper: what it sends, what it accepts, and how it fails.

Every failure here has to arrive as a `MappingError`. The endpoint in T05 turns whatever
this raises into an answer a person reads, and "ReadTimeout" or a pydantic traceback is not
that. So the negative cases assert the type as much as the behaviour.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from vinted_sniper.magic.client import MapperClient
from vinted_sniper.magic.errors import MappingError

GOOD_ANSWER = {
    "catalog": {"id": 2052, "name": "Jackets & Coats"},
    "brand": {"id": 90804, "name": "Patagonia"},
    "sizes": [{"id": 208, "name": "M"}],
    "price_to": "60",
    "currency": "EUR",
    "keywords": ["torrentshell"],
    "visual_signature": "a shell jacket with a hood",
}


class FakeMapper:
    """A stand-in n8n flow: records what arrived, answers with what it was given."""

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


def mapper(fake: FakeMapper, *, token: SecretStr | None = None) -> MapperClient:
    return MapperClient("https://n8n.test/webhook/map", token=token, client=fake.client())


async def test_a_good_answer_becomes_a_mapped_query() -> None:
    fake = FakeMapper()

    mapped = await mapper(fake).map_query("panska bunda Patagonia Torrentshell M do 60 eur", "sk")

    assert mapped.catalog is not None
    assert mapped.catalog.id == 2052
    assert mapped.brand is not None
    assert mapped.brand.id == 90804
    assert [size.id for size in mapped.sizes] == [208]
    assert mapped.keywords == ["torrentshell"]
    assert mapped.to_params()["catalog_ids"] == "2052"


async def test_one_mapping_is_exactly_one_request() -> None:
    """R004's token economy is one mapping call. A retry loop would silently double it."""
    fake = FakeMapper()

    await mapper(fake).map_query("cokolvek", "sk")

    assert fake.calls == 1


async def test_the_request_carries_the_text_the_site_and_the_version() -> None:
    fake = FakeMapper()

    await mapper(fake).map_query("panska bunda", "cz")

    body = json.loads(fake.requests[0].content)
    assert body == {"version": 1, "text": "panska bunda", "tld": "cz"}


async def test_a_configured_token_is_sent_as_a_bearer() -> None:
    fake = FakeMapper()

    await mapper(fake, token=SecretStr("s3cret")).map_query("x", "sk")

    assert fake.requests[0].headers["Authorization"] == "Bearer s3cret"


async def test_no_token_means_no_authorization_header() -> None:
    fake = FakeMapper()

    await mapper(fake).map_query("x", "sk")

    assert "authorization" not in fake.requests[0].headers


async def test_a_500_is_a_mapping_error_naming_the_status() -> None:
    fake = FakeMapper(httpx.Response(500, text="boom"))

    with pytest.raises(MappingError, match="500"):
        await mapper(fake).map_query("x", "sk")


async def test_a_timeout_is_a_mapping_error_not_an_httpx_one() -> None:
    fake = FakeMapper()
    fake.raises = httpx.TimeoutException("read timed out")

    with pytest.raises(MappingError, match="did not answer in time"):
        await mapper(fake).map_query("x", "sk")


async def test_a_connection_failure_is_a_mapping_error() -> None:
    fake = FakeMapper()
    fake.raises = httpx.ConnectError("no route to host")

    with pytest.raises(MappingError, match="could not reach the mapper"):
        await mapper(fake).map_query("x", "sk")


async def test_a_body_that_is_not_json_is_a_mapping_error() -> None:
    """A proxy or a paused flow answers 200 with HTML. That must not surface as a JSON bug."""
    fake = FakeMapper(httpx.Response(200, text="<html>Workflow could not be started</html>"))

    with pytest.raises(MappingError, match="not JSON"):
        await mapper(fake).map_query("x", "sk")


async def test_json_that_is_not_an_object_is_a_mapping_error() -> None:
    fake = FakeMapper(httpx.Response(200, json=[GOOD_ANSWER]))

    with pytest.raises(MappingError, match="not a JSON object"):
        await mapper(fake).map_query("x", "sk")


async def test_a_schema_violation_names_the_field_that_was_wrong() -> None:
    bad = {**GOOD_ANSWER, "brand": {"id": "ninety thousand", "name": "Patagonia"}}
    fake = FakeMapper(httpx.Response(200, json=bad))

    with pytest.raises(MappingError, match=r"brand\.id") as caught:
        await mapper(fake).map_query("x", "sk")

    assert "unusable shape" in str(caught.value)


async def test_a_negative_price_is_refused_by_the_model_not_passed_on() -> None:
    fake = FakeMapper(httpx.Response(200, json={**GOOD_ANSWER, "price_to": "-5"}))

    with pytest.raises(MappingError, match="price_to"):
        await mapper(fake).map_query("x", "sk")


async def test_an_output_wrapper_is_unwrapped() -> None:
    fake = FakeMapper(httpx.Response(200, json={"output": GOOD_ANSWER}))

    mapped = await mapper(fake).map_query("x", "sk")

    assert mapped.brand is not None
    assert mapped.brand.id == 90804


async def test_a_result_wrapper_is_unwrapped() -> None:
    fake = FakeMapper(httpx.Response(200, json={"result": GOOD_ANSWER}))

    mapped = await mapper(fake).map_query("x", "sk")

    assert mapped.catalog is not None
    assert mapped.catalog.id == 2052


async def test_a_wrapper_key_next_to_real_fields_is_not_unwrapped() -> None:
    """Only a lone wrapper key is a wrapper. An answer that also carries a catalog is the
    answer itself, and `extra="ignore"` drops the stray key rather than losing the rest."""
    fake = FakeMapper(httpx.Response(200, json={**GOOD_ANSWER, "output": {"id": 1}}))

    mapped = await mapper(fake).map_query("x", "sk")

    assert mapped.catalog is not None
    assert mapped.catalog.id == 2052


async def test_an_injected_client_is_left_open() -> None:
    fake = FakeMapper()
    client = fake.client()
    subject = MapperClient("https://n8n.test/map", client=client)

    await subject.aclose()

    assert not client.is_closed


async def test_an_owned_client_is_closed() -> None:
    subject = MapperClient("https://n8n.test/map")

    await subject.aclose()

    assert subject._client.is_closed
