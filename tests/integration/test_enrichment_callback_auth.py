"""The enrichment callback's own key.

The verdict flow POSTs from n8n with a bearer token. Giving it the dashboard password
means rotating either secret breaks the other, so the callback accepts a dedicated
token too — one that opens this single endpoint and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from vinted_sniper.config import Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.vinted.models import Item
from vinted_sniper.web.server import create_app

WEB_TOKEN = "web-token-please-ignore"
CALLBACK_TOKEN = "callback-token-please-ignore"

VERDICT = {"score": 80, "verdict": "Worth a look."}


@pytest.fixture
def client(tmp_path: Path, repo: Repo) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "app.db",
        web_enabled=True,
        web_auth_token=SecretStr(WEB_TOKEN),
        callback_auth_token=SecretStr(CALLBACK_TOKEN),
    )
    with TestClient(create_app(settings, repo)) as test_client:
        yield test_client


@pytest.fixture
async def item_id(repo: Repo) -> int:
    query_id = await repo.add_query(
        name="Torrentshell",
        url="https://www.vinted.sk/catalog?search_text=torrentshell",
        tld="sk",
        params={"search_text": "torrentshell"},
        poll_interval_s=60,
    )
    query = await repo.get_query(query_id)
    assert query is not None
    item = Item(
        item_id=1,
        tld="sk",
        title="Bunda",
        url="https://www.vinted.sk/items/1",
        price=Decimal(40),
        total_price=Decimal(45),
        currency="EUR",
        photo_ts=int(time.time()),
    )
    await repo.record_new_items(query, [item], [])
    return item.item_id


def test_the_dedicated_token_can_post_a_verdict(client: TestClient, item_id: int) -> None:
    response = client.post(
        f"/api/items/{item_id}/enrichment",
        json=VERDICT,
        headers={"Authorization": f"Bearer {CALLBACK_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_the_dedicated_token_opens_nothing_else(client: TestClient) -> None:
    response = client.get("/api/export", headers={"Authorization": f"Bearer {CALLBACK_TOKEN}"})

    assert response.status_code == 401


def test_the_web_token_still_works_on_the_callback(client: TestClient, item_id: int) -> None:
    """Back-compat: flows sending the dashboard token keep working during rotation."""
    response = client.post(
        f"/api/items/{item_id}/enrichment",
        json=VERDICT,
        headers={"Authorization": f"Bearer {WEB_TOKEN}"},
    )

    assert response.status_code == 200


def test_a_wrong_token_is_refused(client: TestClient, item_id: int) -> None:
    response = client.post(
        f"/api/items/{item_id}/enrichment",
        json=VERDICT,
        headers={"Authorization": "Bearer not-it"},
    )

    assert response.status_code == 401
