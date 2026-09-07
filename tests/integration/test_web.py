"""The dashboard.

Worth testing properly because it is the part most people will actually touch, and because
it holds webhook URLs and chat ids — so "is it locked" is a correctness question.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.conftest import ScriptedTransport
from vinted_sniper.config import Settings
from vinted_sniper.db import Database
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import sweep
from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.magic.client import MapperClient
from vinted_sniper.magic.models import TriageBatch, TriageItem
from vinted_sniper.magic.triage import TriageClient
from vinted_sniper.magic.verdict import VerdictClient
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.client import PER_PAGE, VintedClient
from vinted_sniper.vinted.models import parse_item
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.taxonomy import Taxonomy
from vinted_sniper.vinted.transport import Response
from vinted_sniper.web.server import SESSION_COOKIE, create_app

TOKEN = "test-token-please-ignore"


@pytest.fixture
def web_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "app.db",
        web_enabled=True,
        web_auth_token=SecretStr(TOKEN),
    )


@pytest.fixture
def client(web_settings: Settings, repo: Repo) -> Iterator[TestClient]:
    with TestClient(create_app(web_settings, repo)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    client.cookies.set(SESSION_COOKIE, TOKEN)
    return client


# --- Access -------------------------------------------------------------------------


def test_the_dashboard_sends_you_to_the_login_page(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/health"),
        ("post", "/searches"),
        ("post", "/destinations"),
        ("post", "/searches/1/delete"),
    ],
)
def test_nothing_useful_works_without_the_token(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path, follow_redirects=False)

    assert response.status_code in (401, 303, 422)
    assert response.status_code != 200


def test_the_wrong_token_is_refused(client: TestClient) -> None:
    response = client.post("/login", data={"access_token": "not-it"}, follow_redirects=False)

    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_the_right_token_signs_you_in(client: TestClient) -> None:
    response = client.post("/login", data={"access_token": TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert response.cookies[SESSION_COOKIE] == TOKEN


def test_the_health_check_needs_no_token(client: TestClient) -> None:
    """The container's health check has no way to sign in."""
    response = client.get("/healthz")

    assert response.status_code in (200, 503)
    assert "alive" in response.json()


def test_without_a_token_the_dashboard_is_open(tmp_path: Path, repo: Repo) -> None:
    """The default for a localhost dashboard: no password, no sign-in page."""
    settings = Settings(_env_file=None, db_path=tmp_path / "a.db", web_enabled=True)  # type: ignore[call-arg]

    with TestClient(create_app(settings, repo)) as client:
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/api/health").status_code == 200
        # The sign-in page has nothing to ask for, so it sends you to the dashboard.
        assert client.get("/login", follow_redirects=False).status_code == 303


async def test_without_a_token_the_feed_needs_no_key(tmp_path: Path, repo: Repo) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "b.db", web_enabled=True)  # type: ignore[call-arg]

    with TestClient(create_app(settings, repo)) as client:
        client.post(
            "/searches",
            data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
            follow_redirects=False,
        )
        query_id = (await repo.list_queries())[0].id
        assert client.get(f"/rss/{query_id}.xml").status_code == 200


# --- Using it -----------------------------------------------------------------------


async def test_adding_a_search_by_pasting_a_url(signed_in: TestClient, repo: Repo) -> None:
    response = signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.fr/catalog?search_text=nike+air&price_to=40&time=999",
            "name": "",
            "interval": "60",
            "max_total_price": "25",
            "banned_keywords": "replica, fake",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    searches = await repo.list_queries()
    assert len(searches) == 1
    assert searches[0].tld == "fr"
    assert searches[0].max_total_price is not None
    assert searches[0].banned_keywords == ["replica", "fake"]
    assert "time=999" not in searches[0].url, "tracking parameters should not survive"


async def test_a_url_that_is_not_a_search_is_rejected_with_advice(
    signed_in: TestClient, repo: Repo
) -> None:
    response = signed_in.post(
        "/searches", data={"url": "https://example.com/nope"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert await repo.list_queries() == []


async def test_the_same_search_cannot_be_added_twice(signed_in: TestClient, repo: Repo) -> None:
    payload = {"url": "https://www.vinted.fr/catalog?search_text=nike"}
    signed_in.post("/searches", data=payload, follow_redirects=False)
    response = signed_in.post("/searches", data=payload, follow_redirects=False)

    assert "error=" in response.headers["location"]
    assert len(await repo.list_queries()) == 1


async def test_a_search_below_the_interval_floor_is_raised_to_it(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": "1"},
        follow_redirects=False,
    )

    searches = await repo.list_queries()
    assert searches[0].poll_interval_s >= 10


async def test_pausing_and_deleting_a_search(signed_in: TestClient, repo: Repo) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
        follow_redirects=False,
    )
    query_id = (await repo.list_queries())[0].id

    signed_in.post(f"/searches/{query_id}/pause", data={"paused": "1"}, follow_redirects=False)
    assert (await repo.list_queries())[0].paused is True

    signed_in.post(f"/searches/{query_id}/delete", follow_redirects=False)
    assert await repo.list_queries() == []


async def test_adding_a_discord_destination(signed_in: TestClient, repo: Repo) -> None:
    signed_in.post(
        "/destinations",
        data={
            "kind": "discord",
            "name": "my server",
            "target": "https://discord.com/api/webhooks/1/abc",
        },
        follow_redirects=False,
    )

    destinations = await repo.list_destinations()
    assert len(destinations) == 1
    assert destinations[0].config["webhook_url"].startswith("https://discord.com/")


async def test_a_discord_destination_that_is_not_a_url_is_refused(
    signed_in: TestClient, repo: Repo
) -> None:
    response = signed_in.post(
        "/destinations",
        data={"kind": "discord", "name": "typo", "target": "my-webhook"},
        follow_redirects=False,
    )

    assert "error=" in response.headers["location"]
    assert await repo.list_destinations() == []


async def test_the_dashboard_shows_what_is_being_watched(signed_in: TestClient, repo: Repo) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "name": "my search"},
        follow_redirects=False,
    )

    body = signed_in.get("/searches").text

    assert "my search" in body
    assert "vinted.fr" in body


async def test_found_listings_render_as_cards_with_their_gallery(
    signed_in: TestClient, repo: Repo, make_item: Callable[..., dict[str, Any]]
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "name": "my search"},
        follow_redirects=False,
    )
    query = (await repo.list_queries())[0]
    item = parse_item(make_item(123, photo_ts=1_755_000_000), "fr")
    await repo.record_new_items(query, [item], [])

    body = signed_in.get("/").text

    assert "listing-grid" in body
    # The whole gallery travels to the page so the lightbox needs no more requests.
    assert "https://images.vinted.net/123.jpeg" in body
    assert "123-back.jpeg" in body
    assert "2 ▣" in body
    assert "@seller" in body
    assert "⭐ 4.5" in body  # feedback_reputation 0.9, on the five-star scale
    assert "Nike" in body


def test_the_health_api_answers_with_the_snapshot(signed_in: TestClient) -> None:
    body = signed_in.get("/api/health").json()

    assert set(body) >= {"alive", "searches", "queued_notifications"}


async def test_the_rss_feed_needs_the_token(signed_in: TestClient, repo: Repo) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
        follow_redirects=False,
    )
    query_id = (await repo.list_queries())[0].id

    assert signed_in.get(f"/rss/{query_id}.xml").status_code == 401

    response = signed_in.get(f"/rss/{query_id}.xml?key={TOKEN}")
    assert response.status_code == 200
    assert response.text.startswith("<?xml")


# --- The advanced-search builder ------------------------------------------------------


@pytest.fixture
def builder_client(
    web_settings: Settings, db: Database, repo: Repo, transport: ScriptedTransport
) -> Iterator[TestClient]:
    """A dashboard wired to a taxonomy service that talks to a scripted Vinted."""
    taxonomy = Taxonomy(SessionManager(db, transport), repo)
    with TestClient(create_app(web_settings, repo, taxonomy)) as test_client:
        test_client.cookies.set(SESSION_COOKIE, TOKEN)
        yield test_client


def _page_with_tree() -> Response:
    payload = {
        "CSRF_TOKEN": "11112222-3333-4444",
        "catalogTree": [{"id": 1904, "title": "Women", "catalogs": []}],
    }
    html = f"<script>self.__next_f.push([1,{json.dumps(json.dumps(payload))}])</script>"
    return Response(status_code=200, text=html, headers={}, cookies={"access_token_web": "t"})


def test_the_dashboard_offers_the_builder_when_the_service_is_wired(
    builder_client: TestClient, signed_in: TestClient
) -> None:
    assert "Build a search instead" in builder_client.get("/searches").text
    assert "Build a search instead" not in signed_in.get("/searches").text


def test_the_filter_endpoints_need_a_login(client: TestClient) -> None:
    for path in (
        "/api/filters/fr/categories",
        "/api/filters/fr/brands?q=nike",
        "/api/filters/fr/facets/status",
    ):
        assert client.get(path).status_code == 401, path


def test_without_the_service_the_builder_answers_503(signed_in: TestClient) -> None:
    assert signed_in.get("/api/filters/fr/categories").status_code == 503


def test_categories_come_back_as_a_tree(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    transport.queue_root(_page_with_tree())  # session bootstrap
    transport.queue_root(_page_with_tree())  # the page that carries the tree

    response = builder_client.get("/api/filters/fr/categories")

    assert response.status_code == 200
    assert response.json() == {"categories": [{"id": 1904, "title": "Women", "children": []}]}


def test_an_unknown_site_is_refused_before_talking_to_vinted(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    assert builder_client.get("/api/filters/xx/categories").status_code == 404
    assert transport.requests == []


def test_a_short_brand_query_is_answered_locally(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    response = builder_client.get("/api/filters/fr/brands?q=n")

    assert response.json() == {"brands": []}
    assert transport.requests == []


def test_brands_pass_through_with_ids_and_counts(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    transport.queue(
        Response(
            status_code=200,
            text=json.dumps({"brands": [{"id": 53, "title": "Nike", "item_count": 9}]}),
            headers={},
            cookies={},
        )
    )

    response = builder_client.get("/api/filters/fr/brands?q=nike")

    assert response.json() == {"brands": [{"id": 53, "title": "Nike", "count": 9}]}


def test_junk_in_catalog_ids_never_reaches_vinted(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    transport.queue_root(_page_with_tree())
    transport.queue_root(_page_with_tree())
    transport.queue(
        Response(status_code=200, text=json.dumps({"options": []}), headers={}, cookies={})
    )

    builder_client.get("/api/filters/fr/brands?q=nike&catalog_ids=12,drop%20table,34")

    assert transport.requests[-1]["params"]["catalog_ids"] == "12,34"


def test_an_unknown_facet_is_a_404(builder_client: TestClient) -> None:
    assert builder_client.get("/api/filters/fr/facets/shoe_smell").status_code == 404


def test_a_refusal_from_vinted_surfaces_as_a_502_with_the_reason(
    builder_client: TestClient, transport: ScriptedTransport
) -> None:
    transport.queue_root(_page_with_tree())
    transport.queue_root(_page_with_tree())
    transport.queue_status(403, "blocked")

    response = builder_client.get("/api/filters/fr/facets/status")

    assert response.status_code == 502
    assert "error" in response.json()


async def test_a_search_can_be_edited_and_the_change_marks_it_for_restart(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": "60"},
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()
    before = query.updated_at

    response = signed_in.post(
        f"/searches/{query.id}/edit",
        data={
            "name": "Nike, cheap",
            "interval": "300",
            "max_total_price": "25",
            "required_keywords": "air max, 90",
            "title_pattern": r"\b4[0-2]\b",
            "min_seller_rating": "90",
            "min_seller_reviews": "5",
            "blocked_sellers": "scammer",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and "error=" not in response.headers["location"]

    edited = await repo.get_query(query.id)
    assert edited is not None
    assert edited.name == "Nike, cheap"
    assert edited.poll_interval_s == 300
    assert edited.max_total_price == Decimal("25")
    assert edited.required_keywords == ["air max", "90"]
    assert edited.title_pattern == r"\b4[0-2]\b"
    assert edited.min_seller_rating == 0.9
    assert edited.min_seller_reviews == 5
    assert edited.blocked_sellers == ["scammer"]
    assert edited.url == query.url, "the URL is the search's identity and stays put"
    assert edited.updated_at >= before


async def test_an_edit_with_a_broken_regex_is_refused_without_touching_the_search(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()

    response = signed_in.post(
        f"/searches/{query.id}/edit", data={"title_pattern": "("}, follow_redirects=False
    )

    assert "error=" in response.headers["location"]
    same = await repo.get_query(query.id)
    assert same is not None and same.title_pattern is None


async def test_every_interval_can_be_changed_at_once(signed_in: TestClient, repo: Repo) -> None:
    for text in ("a", "b", "c"):
        signed_in.post(
            "/searches",
            data={"url": f"https://www.vinted.fr/catalog?search_text={text}"},
            follow_redirects=False,
        )

    signed_in.post("/searches/interval", data={"interval": "240"}, follow_redirects=False)

    assert {q.poll_interval_s for q in await repo.list_queries()} == {240}


def test_the_health_feed_carries_what_the_live_table_needs(signed_in: TestClient) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
        follow_redirects=False,
    )
    body = signed_in.get("/api/health").json()
    (search,) = body["searches"]
    for key in ("state", "items_total", "last_success_at", "next_check_at", "poll_interval_s"):
        assert key in search


async def test_the_history_page_shows_what_was_queued_and_where(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike"},
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    item = parse_item(
        {
            "id": 77,
            "title": "Nike Air Max 90",
            "url": "https://www.vinted.fr/items/77",
            "price": {"amount": "30.0", "currency_code": "EUR"},
            "photo": {
                "full_size_url": "https://images.vinted.net/77.jpeg",
                "high_resolution": {"timestamp": 1},
            },
        },
        "fr",
    )
    await repo.record_new_items(query, [item], [destination_id])

    page = signed_in.get("/history")
    assert page.status_code == 200
    assert "Nike Air Max 90" in page.text
    assert "phone" in page.text
    assert "pending" in page.text

    body = signed_in.get("/api/history", params={"status": "pending"}).json()
    assert body["deliveries"][0]["title"] == "Nike Air Max 90"
    assert signed_in.get("/api/history", params={"status": "sent"}).json()["deliveries"] == []


async def test_a_search_can_be_cloned_to_another_country_with_its_filters_and_routing(
    signed_in: TestClient, repo: Repo
) -> None:
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.sk/catalog?search_text=rab%20downpour&brand_ids[]=53",
            "max_total_price": "60",
            "max_market_percentile": "25",
            "destination_ids": [str(destination_id)],
        },
        follow_redirects=False,
    )
    (source,) = await repo.list_queries()

    response = signed_in.post(
        f"/searches/{source.id}/clone", data={"tld": "de"}, follow_redirects=False
    )
    assert "ok=" in response.headers["location"]

    _, clone = await repo.list_queries()
    assert clone.tld == "de"
    assert clone.url.startswith("https://www.vinted.de/catalog?")
    assert clone.params == source.params
    assert clone.max_total_price == source.max_total_price
    assert clone.max_market_percentile == 25
    assert clone.name.endswith("(de)") and "(sk) (de)" not in clone.name
    assert await repo.destination_ids_for_query(clone.id) == [destination_id]

    again = signed_in.post(
        f"/searches/{source.id}/clone", data={"tld": "de"}, follow_redirects=False
    )
    assert "error=" in again.headers["location"], "cloning twice is refused"
    same_site = signed_in.post(
        f"/searches/{source.id}/clone", data={"tld": "sk"}, follow_redirects=False
    )
    assert "error=" in same_site.headers["location"]


def test_the_help_page_explains_the_words(signed_in: TestClient) -> None:
    page = signed_in.get("/help")
    assert page.status_code == 200
    for phrase in ("Only the cheapest", "Total price", "Quiet hours", "Current settings"):
        assert phrase in page.text


# --- Security --------------------------------------------------------------------------


def test_every_response_carries_the_security_headers(signed_in: TestClient) -> None:
    response = signed_in.get("/")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "form-action 'self'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_a_form_posted_from_another_site_is_refused(signed_in: TestClient, repo: Repo) -> None:
    payload = {"url": "https://www.vinted.fr/catalog?search_text=csrf"}
    response = signed_in.post(
        "/searches",
        data=payload,
        headers={"Origin": "https://evil.example", "Referer": "https://evil.example/page"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    same = signed_in.post(
        "/searches", data=payload, headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert same.status_code == 303


def test_wrong_tokens_are_throttled(client: TestClient) -> None:
    for _ in range(5):
        assert client.post("/login", data={"access_token": "nope"}).status_code == 401
    blocked = client.post("/login", data={"access_token": "nope"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_the_session_cookie_is_httponly_and_strict(client: TestClient) -> None:
    response = client.post("/login", data={"access_token": TOKEN}, follow_redirects=False)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_no_inline_event_handlers_in_the_templates() -> None:
    """A name with a quote in it must stay text; inline handlers turn it into code."""

    templates = Path(__file__).resolve().parents[2] / "src/vinted_sniper/web/templates"
    for page in templates.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert " onsubmit=" not in text and " onclick=" not in text, page.name


# --- Form abuse: empty and garbage input never surfaces a raw 422 ---------------------
# Every HTML form submits its empty fields as empty strings, and the "all" options of
# the history filter submit search= with no value. None of that may leak FastAPI's raw
# JSON validation error at the user; the worst allowed outcome is a redirect with error=.


def test_the_history_filter_set_to_all_is_not_an_error(signed_in: TestClient) -> None:
    """The exact submit the Filter button makes with both selects on "all"."""
    response = signed_in.get("/history", params={"search": "", "status": ""})

    assert response.status_code == 200
    assert "Delivery history" in response.text


def test_garbage_in_the_history_search_filter_falls_back_to_all(signed_in: TestClient) -> None:
    response = signed_in.get("/history", params={"search": "abc", "status": "sent"})

    assert response.status_code == 200


def test_the_history_api_tolerates_the_same_empty_filters(signed_in: TestClient) -> None:
    response = signed_in.get("/api/history", params={"search": "", "status": ""})

    assert response.status_code == 200
    assert "deliveries" in response.json()


async def test_adding_a_search_with_a_cleared_interval_uses_the_default(
    signed_in: TestClient, repo: Repo, web_settings: Settings
) -> None:
    response = signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303 and "error=" not in response.headers["location"]
    (query,) = await repo.list_queries()
    assert query.poll_interval_s == web_settings.poll_default_interval_s


async def test_adding_a_search_with_a_garbage_interval_uses_the_default(
    signed_in: TestClient, repo: Repo, web_settings: Settings
) -> None:
    response = signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": "soon"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    (query,) = await repo.list_queries()
    assert query.poll_interval_s == web_settings.poll_default_interval_s


async def test_editing_with_a_cleared_interval_keeps_the_old_one(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": "300"},
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()

    response = signed_in.post(
        f"/searches/{query.id}/edit", data={"interval": ""}, follow_redirects=False
    )

    assert response.status_code == 303 and "error=" not in response.headers["location"]
    edited = await repo.get_query(query.id)
    assert edited is not None and edited.poll_interval_s == 300


async def test_editing_a_search_updates_its_destinations(signed_in: TestClient, repo: Repo) -> None:
    telegram_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    webhook_id = await repo.add_destination(
        kind="webhook", name="agent", config={"url": "http://x"}
    )
    signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.fr/catalog?search_text=nike",
            "destination_ids": [str(telegram_id)],
        },
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()
    assert await repo.destination_ids_for_query(query.id) == [telegram_id]

    response = signed_in.post(
        f"/searches/{query.id}/edit",
        data={"destination_ids": [str(webhook_id)]},
        follow_redirects=False,
    )

    assert response.status_code == 303 and "error=" not in response.headers["location"]
    assert await repo.destination_ids_for_query(query.id) == [webhook_id]


async def test_editing_a_search_with_no_boxes_ticked_unroutes_it(
    signed_in: TestClient, repo: Repo
) -> None:
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.fr/catalog?search_text=nike",
            "destination_ids": [str(destination_id)],
        },
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()

    signed_in.post(f"/searches/{query.id}/edit", data={}, follow_redirects=False)

    assert await repo.destination_ids_for_query(query.id) == []


async def test_apply_to_all_with_a_blank_interval_says_so_instead_of_crashing(
    signed_in: TestClient, repo: Repo
) -> None:
    signed_in.post(
        "/searches",
        data={"url": "https://www.vinted.fr/catalog?search_text=nike", "interval": "300"},
        follow_redirects=False,
    )

    for bad in ("", "fast", "  "):
        response = signed_in.post(
            "/searches/interval", data={"interval": bad}, follow_redirects=False
        )
        assert response.status_code == 303, bad
        assert "error=" in response.headers["location"], bad
    assert (await repo.list_queries())[0].poll_interval_s == 300, "nothing was changed"


async def test_every_form_survives_all_optional_fields_submitted_empty(
    signed_in: TestClient, repo: Repo
) -> None:
    """One pass over each mutating form with every optional field as the browser sends
    it when left blank: present, empty. A raw 422 anywhere here is a regression."""
    signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.fr/catalog?search_text=nike",
            "name": "",
            "interval": "",
            "max_total_price": "",
            "banned_keywords": "",
            "required_keywords": "",
            "title_pattern": "",
            "min_seller_rating": "",
            "min_seller_reviews": "",
            "blocked_sellers": "",
            "max_market_percentile": "",
        },
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})

    empty_edit = {
        "name": "",
        "interval": "",
        "max_total_price": "",
        "banned_keywords": "",
        "required_keywords": "",
        "title_pattern": "",
        "min_seller_rating": "",
        "min_seller_reviews": "",
        "blocked_sellers": "",
        "max_market_percentile": "",
    }
    attempts = [
        ("post", f"/searches/{query.id}/edit", empty_edit),
        ("post", f"/searches/{query.id}/pause", {"paused": "1"}),
        ("post", f"/destinations/{destination_id}/quiet", {"quiet_hours": ""}),
        ("post", f"/destinations/{destination_id}/language", {"language": "en"}),
        ("post", f"/destinations/{destination_id}/status", {"enabled": "1"}),
        ("get", "/history", {"search": "", "status": ""}),
        ("get", "/api/history", {"search": "", "status": ""}),
    ]
    for method, path, payload in attempts:
        if method == "get":
            response = signed_in.get(path, params=payload)
        else:
            response = signed_in.post(path, data=payload, follow_redirects=False)
        assert response.status_code in (200, 303), f"{method} {path} -> {response.status_code}"


# --- Destination edit, enable, delete -----------------------------------------------


async def test_a_destination_can_be_edited(signed_in: TestClient, repo: Repo) -> None:
    destination_id = await repo.add_destination(
        kind="ntfy", name="phone", config={"topic": "old-topic"}
    )

    response = signed_in.post(
        f"/destinations/{destination_id}/edit",
        data={"name": "tablet", "target": "new-topic"},
        follow_redirects=False,
    )

    assert response.status_code == 303 and "ok=" in response.headers["location"]
    edited = await repo.get_destination(destination_id)
    assert edited is not None
    assert edited.name == "tablet"
    assert edited.config == {"topic": "new-topic"}


async def test_editing_keeps_kind_validation(signed_in: TestClient, repo: Repo) -> None:
    destination_id = await repo.add_destination(
        kind="discord",
        name="server",
        config={"webhook_url": "https://discord.com/api/webhooks/1/a"},
    )

    response = signed_in.post(
        f"/destinations/{destination_id}/edit",
        data={"name": "server", "target": "not-a-url"},
        follow_redirects=False,
    )

    assert "error=" in response.headers["location"]
    same = await repo.get_destination(destination_id)
    assert same is not None and same.config["webhook_url"].startswith("https://")


async def test_a_disabled_destination_can_be_enabled_again(
    signed_in: TestClient, repo: Repo
) -> None:
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.deactivate_destination(destination_id, "the far end vanished")

    response = signed_in.post(f"/destinations/{destination_id}/enable", follow_redirects=False)

    assert response.status_code == 303 and "ok=" in response.headers["location"]
    destination = await repo.get_destination(destination_id)
    assert destination is not None and destination.active


async def test_deleting_a_destination_removes_it_even_when_disabled(
    signed_in: TestClient, repo: Repo
) -> None:
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    await repo.deactivate_destination(destination_id, "gone")

    response = signed_in.post(f"/destinations/{destination_id}/delete", follow_redirects=False)

    assert response.status_code == 303 and "ok=" in response.headers["location"]
    assert await repo.list_destinations() == []


async def test_deleting_a_destination_unroutes_it(signed_in: TestClient, repo: Repo) -> None:
    destination_id = await repo.add_destination(kind="ntfy", name="phone", config={"topic": "t"})
    signed_in.post(
        "/searches",
        data={
            "url": "https://www.vinted.fr/catalog?search_text=nike",
            "destination_ids": [str(destination_id)],
        },
        follow_redirects=False,
    )
    (query,) = await repo.list_queries()
    assert await repo.destination_ids_for_query(query.id) == [destination_id]

    signed_in.post(f"/destinations/{destination_id}/delete", follow_redirects=False)

    assert await repo.destination_ids_for_query(query.id) == []
    assert await repo.list_destinations() == []


# --- Magic Search ---------------------------------------------------------------------
# The endpoint sits between two things that can lie: an n8n flow that may be down, and a
# language model that will happily invent a category id. Both have to arrive as a readable
# 422, and the params that do come out have to be a search — never the title rules.

MAPPED_ANSWER: dict[str, Any] = {
    "catalog": {"id": 2052, "name": "Jackets & Coats"},
    "brand": {"id": 90804, "name": "Patagonia"},
    "sizes": [{"id": 208, "name": "M"}],
    "price_to": "60",
    "currency": "EUR",
    "search_text": "patagonia torrentshell",
    "keywords": ["torrentshell"],
    "visual_signature": "a hooded shell jacket, one plain colour, taped seams",
    "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": r"\btorrentshell\b"},
}


class FakeFlow:
    """A stand-in n8n mapper: answers with what it was given, or fails how it was told to."""

    def __init__(
        self, answer: dict[str, Any] | None = None, *, raises: Exception | None = None
    ) -> None:
        self.answer = MAPPED_ANSWER if answer is None else answer
        self.raises = raises
        self.calls = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return httpx.Response(200, json=self.answer)

    def mapper(self) -> MapperClient:
        return MapperClient(
            "https://n8n.test/webhook/magic",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )


@pytest.fixture
def magic_client(
    web_settings: Settings, db: Database, repo: Repo, transport: ScriptedTransport
) -> Iterator[Callable[[FakeFlow], TestClient]]:
    """A signed-in dashboard wired to a scripted Vinted and whichever fake flow a test wants."""
    with contextlib.ExitStack() as stack:

        def build(flow: FakeFlow) -> TestClient:
            taxonomy = Taxonomy(SessionManager(db, transport), repo)
            test_client = stack.enter_context(
                TestClient(create_app(web_settings, repo, taxonomy, flow.mapper()))
            )
            test_client.cookies.set(SESSION_COOKIE, TOKEN)
            return test_client

        yield build


def _page_with_jackets() -> Response:
    payload = {
        "CSRF_TOKEN": "11112222-3333-4444",
        "catalogTree": [
            {
                "id": 5,
                "title": "Men",
                "catalogs": [{"id": 2052, "title": "Jackets & Coats", "catalogs": []}],
            }
        ],
    }
    html = f"<script>self.__next_f.push([1,{json.dumps(json.dumps(payload))}])</script>"
    return Response(status_code=200, text=html, headers={}, cookies={"access_token_web": "t"})


def _api_response(payload: dict[str, Any]) -> Response:
    return Response(status_code=200, text=json.dumps(payload), headers={}, cookies={})


def _script_the_id_check(transport: ScriptedTransport) -> None:
    """Line up what validation reads: the category tree, then the brand and size facets."""
    transport.queue_root(_page_with_jackets())  # session bootstrap
    transport.queue_root(_page_with_jackets())  # the page that carries the tree and the token
    transport.queue(_api_response({"options": [{"id": 90804, "title": "Patagonia"}]}))
    transport.queue(_api_response({"options": [{"id": 208, "title": "M"}]}))


def test_magic_search_needs_a_login(client: TestClient) -> None:
    response = client.post(
        "/api/magic-search/map", json={"text": "panska bunda Patagonia", "tld": "sk"}
    )

    assert response.status_code == 401


def test_without_a_mapper_magic_search_says_it_is_not_set_up(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/magic-search/map", json={"text": "panska bunda Patagonia", "tld": "sk"}
    )

    assert response.status_code == 503
    assert "not set up" in response.json()["detail"]


def test_without_a_taxonomy_the_ids_cannot_be_checked_so_magic_search_refuses(
    web_settings: Settings, repo: Repo
) -> None:
    flow = FakeFlow()
    with TestClient(create_app(web_settings, repo, None, flow.mapper())) as test_client:
        test_client.cookies.set(SESSION_COOKIE, TOKEN)

        response = test_client.post(
            "/api/magic-search/map", json={"text": "panska bunda Patagonia", "tld": "sk"}
        )

    assert response.status_code == 503
    assert flow.calls == 0  # nothing is asked of the AI when the answer could not be checked


def test_a_sentence_becomes_the_params_a_search_already_takes(
    magic_client: Callable[[FakeFlow], TestClient], transport: ScriptedTransport
) -> None:
    _script_the_id_check(transport)

    response = magic_client(FakeFlow()).post(
        "/api/magic-search/map",
        json={"text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["params"] == {
        "catalog_ids": "2052",
        "brand_ids": "90804",
        "size_ids": "208",
        "price_to": "60",
        "currency": "EUR",
        "search_text": "patagonia torrentshell",
        "order": "newest_first",
    }
    assert body["tld"] == "sk"
    assert body["keywords"] == ["torrentshell"]
    assert body["visual_signature"]
    # The names ride along so a confirmation screen can show words, not three integers.
    assert body["labels"] == {
        "catalog": "Jackets & Coats",
        "brand": "Patagonia",
        "sizes": ["M"],
    }


def test_the_title_rules_travel_beside_the_params_never_inside_them(
    magic_client: Callable[[FakeFlow], TestClient], transport: ScriptedTransport
) -> None:
    """R003: a sweep ranks on the title, it never filters on it — so the hints stay out."""
    _script_the_id_check(transport)

    body = (
        magic_client(FakeFlow())
        .post("/api/magic-search/map", json={"text": "patagonia bunda", "tld": "sk"})
        .json()
    )

    assert "required_keywords" not in body["params"]
    assert "title_pattern" not in body["params"]
    assert body["watch_hints"]["required_keywords"] == ["torrentshell"]
    assert body["watch_hints"]["title_pattern"]


def test_a_category_the_model_invented_is_refused_by_number(
    magic_client: Callable[[FakeFlow], TestClient], transport: ScriptedTransport
) -> None:
    transport.queue_root(_page_with_jackets())  # session bootstrap
    transport.queue_root(_page_with_jackets())  # the tree, which has no catalog 9999
    invented = {**MAPPED_ANSWER, "catalog": {"id": 9999, "name": "Jackets & Coats"}}

    response = magic_client(FakeFlow(invented)).post(
        "/api/magic-search/map", json={"text": "panska bunda", "tld": "sk"}
    )

    assert response.status_code == 422
    assert "9999" in response.json()["error"]
    assert transport.requests[-1]["url"].endswith("/catalog")  # no brand or size call followed


def test_a_mapper_that_cannot_be_reached_is_a_422_in_plain_words(
    magic_client: Callable[[FakeFlow], TestClient], transport: ScriptedTransport
) -> None:
    flow = FakeFlow(raises=httpx.ConnectError("nodename nor servname provided"))

    response = magic_client(flow).post(
        "/api/magic-search/map", json={"text": "panska bunda", "tld": "sk"}
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert "could not reach the mapper" in response.json()["error"]
    assert "Traceback" not in response.text
    assert transport.requests == []  # a failed mapping never touches Vinted


def test_an_unknown_site_is_refused_before_the_mapper_is_called(
    magic_client: Callable[[FakeFlow], TestClient],
) -> None:
    flow = FakeFlow()

    response = magic_client(flow).post(
        "/api/magic-search/map", json={"text": "panska bunda", "tld": "xx"}
    )

    assert response.status_code == 404
    assert flow.calls == 0


# --- Judged sweeps ------------------------------------------------------------------
#
# The read surface for the milestone: one endpoint S04 will poll while a sweep is still
# running, and a plain section on /history so a judged sweep is visible without SQLite.


async def _seed_judged_sweep(repo: Repo) -> int:
    """One sweep, two candidates: a photo-check match with a full opinion, and a rejection.

    Written through the repo writers the engine uses, not with raw SQL, so this seeds the
    same rows a real judged run would leave behind.
    """
    sweep_id = await repo.create_sweep_run(
        tld="sk", params={"search_text": "torrentshell"}, keywords=["torrentshell"]
    )
    match = parse_item(
        {
            "id": 5551234,
            # The title never names the model — the whole reason the photo check exists.
            "title": "Panska bunda M",
            "url": "https://www.vinted.sk/items/5551234",
            "price": {"amount": "48.0", "currency_code": "EUR"},
            "photo": {
                "full_size_url": "https://images.vinted.net/5551234.jpeg",
                "high_resolution": {"timestamp": 1},
                "thumbnails": [
                    {
                        "type": "thumb310x430",
                        "width": 310,
                        "height": 430,
                        "url": "https://images.vinted.net/5551234-310.jpeg",
                    }
                ],
            },
        },
        "sk",
    )
    reject = parse_item(
        {
            "id": 5559876,
            "title": "Torrentshell fleece",
            "url": "https://www.vinted.sk/items/5559876",
            "price": {"amount": "20.0", "currency_code": "EUR"},
            "photo": {
                "full_size_url": "https://images.vinted.net/5559876.jpeg",
                "high_resolution": {"timestamp": 1},
            },
        },
        "sk",
    )
    await repo.record_sweep_candidates(
        sweep_id,
        [
            sweep._to_candidate(sweep.RankedItem(item=match, rank_score=0.0), 0),
            sweep._to_candidate(sweep.RankedItem(item=reject, rank_score=1.0), 1),
        ],
    )
    await repo.record_triage(
        sweep_id,
        [
            TriageItem(
                id=5551234,
                matches_target=True,
                confidence=0.86,
                reason="Grey three-layer shell with the hood described.",
            ),
            TriageItem(
                id=5559876,
                matches_target=False,
                confidence=0.71,
                reason="A fleece, not a shell jacket.",
            ),
        ],
    )
    await repo.record_verdict(
        sweep_id,
        5551234,
        EnrichmentIn(
            score=82,
            model="Patagonia Torrentshell 3L",
            retail_price=Decimal("180"),
            matches_query=True,
            verdict="Genuine, and well under what it usually goes for.",
        ),
        1_760_000_000,
    )
    await repo.add_sweep_cost(sweep_id, 41840, 0.0421)
    await repo.finish_sweep_run(
        sweep_id,
        status="ok",
        pages_fetched=2,
        items_seen=40,
        candidates=2,
        funnel={"over budget": 6},
    )
    return sweep_id


async def test_a_sweep_cannot_be_read_without_signing_in(client: TestClient, repo: Repo) -> None:
    sweep_id = await _seed_judged_sweep(repo)

    response = client.get(f"/api/sweeps/{sweep_id}")

    assert response.status_code == 401


def test_an_unknown_sweep_is_a_404(signed_in: TestClient) -> None:
    assert signed_in.get("/api/sweeps/4242").status_code == 404


async def test_a_judged_sweep_comes_back_whole(signed_in: TestClient, repo: Repo) -> None:
    sweep_id = await _seed_judged_sweep(repo)

    body = signed_in.get(f"/api/sweeps/{sweep_id}").json()

    assert body["status"] == "ok"
    assert (body["pages_fetched"], body["items_seen"], body["kept"]) == (2, 40, 2)
    assert (body["triaged"], body["verdicts"]) == (2, 1)
    assert (body["tokens"], body["cost_eur"]) == (41840, 0.0421)
    assert body["funnel"] == {"over budget": 6}

    # The photo check's match leads, even though its title scored 0.0 and the rejected
    # listing's title scored 1.0. That inversion is the slice.
    first, second = body["candidates"]
    assert (first["item_id"], first["rank_score"]) == (5551234, 0.0)
    assert (second["item_id"], second["rank_score"]) == (5559876, 1.0)

    assert first["matches_target"] is True
    assert first["confidence"] == 0.86
    assert first["triage_reason"] == "Grey three-layer shell with the hood described."
    assert first["thumb_url"] == "https://images.vinted.net/5551234-310.jpeg"
    assert first["url"] == "https://www.vinted.sk/items/5551234"
    assert first["price"] == 48.0
    assert first["verdict"]["score"] == 82
    assert first["verdict"]["model"] == "Patagonia Torrentshell 3L"
    assert first["verdict"]["matches_query"] is True
    assert "deal 82/100" in first["verdict"]["summary"]

    # Rejected, and no opinion was bought for it — `None`, not an object full of nulls.
    assert second["matches_target"] is False
    assert second["verdict"] is None


async def test_an_untriaged_candidate_keeps_its_third_value(
    signed_in: TestClient, repo: Repo
) -> None:
    """`null` is "nobody looked", which is not "looked and said no"."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=[])
    item = parse_item(
        {
            "id": 42,
            "title": "Bunda",
            "url": "https://www.vinted.sk/items/42",
            "price": {"amount": "10.0", "currency_code": "EUR"},
            "photo": {"full_size_url": "x", "high_resolution": {"timestamp": 1}},
        },
        "sk",
    )
    await repo.record_sweep_candidates(
        sweep_id, [sweep._to_candidate(sweep.RankedItem(item=item, rank_score=0.5), 0)]
    )

    body = signed_in.get(f"/api/sweeps/{sweep_id}").json()

    assert body["triaged"] == 0
    assert body["candidates"][0]["matches_target"] is None
    assert body["candidates"][0]["verdict"] is None


async def test_the_history_page_shows_a_judged_sweep_with_its_scores_and_cost(
    signed_in: TestClient, repo: Repo
) -> None:
    await _seed_judged_sweep(repo)

    page = signed_in.get("/history")

    assert page.status_code == 200
    assert "Judged sweeps" in page.text
    assert "Panska bunda M" in page.text
    assert "https://www.vinted.sk/items/5551234" in page.text
    assert "looks like it, 86% sure" in page.text
    assert "Grey three-layer shell with the hood described." in page.text
    assert "82" in page.text
    assert "41840 tokens billed — €0.0421" in page.text


async def test_an_unjudged_sweep_stays_off_the_history_page(
    signed_in: TestClient, repo: Repo
) -> None:
    """A `sweep` run without --judge has no scores and no bill, so it has no row here."""
    sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=["nike"])
    await repo.finish_sweep_run(
        sweep_id, status="ok", pages_fetched=1, items_seen=3, candidates=0, funnel={}
    )

    page = signed_in.get("/history")

    assert "Judged sweeps" not in page.text


# --- The sweep's dependencies ---------------------------------------------------------


def test_the_dashboard_carries_everything_a_sweep_needs(
    web_settings: Settings, db: Database, repo: Repo, transport: ScriptedTransport
) -> None:
    """`judge_sweep()` wants four things the web process never used to hold.

    Building the app with all four has to work without a single call to Vinted — the
    clients are handed in already built, so nothing here opens a connection.
    """
    sessions = SessionManager(db, transport)
    vinted = VintedClient(transport, sessions)
    triage = TriageClient("https://n8n.example/triage", timeout_s=5.0)
    verdict = VerdictClient("https://n8n.example/verdict", timeout_s=5.0)

    app = create_app(
        web_settings,
        repo,
        None,
        None,
        client=vinted,
        sessions=sessions,
        triage=triage,
        verdict=verdict,
    )

    assert app.state.vinted is vinted
    assert app.state.sessions is sessions
    assert app.state.triage is triage
    assert app.state.verdict is verdict
    assert transport.requests == []


def test_a_dashboard_built_without_them_still_starts(web_settings: Settings, repo: Repo) -> None:
    """The four are optional on purpose: most of the dashboard has no use for them.

    An install with no Magic webhooks set, and every existing test that calls
    `create_app(settings, repo)`, land here.
    """
    app = create_app(web_settings, repo)

    assert app.state.vinted is None
    assert app.state.sessions is None
    assert app.state.triage is None
    assert app.state.verdict is None


# --- Launching a sweep ----------------------------------------------------------------
#
# The slice's one vertical: a POST that answers before the work is done, an id the page can
# poll while it runs, and no second bill for a second click. Nothing here sleeps — the
# launcher is injected, so the test decides exactly when the sweep runs and reads the same
# `GET /api/sweeps/{id}` the browser will.


class CollectingLauncher:
    """Takes the sweep's coroutine and holds it, so the test runs it when it chooses.

    The alternative is `asyncio.create_task` plus a sleep, which races `TestClient`'s own
    thread and would flake. Holding the coroutine makes "before the sweep ran" and "after
    the sweep ran" two lines of a test instead of a guess about timing.
    """

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def __call__(self, coro: Any) -> None:
        self.pending.append(coro)

    async def drain(self) -> None:
        while self.pending:
            await self.pending.pop(0)

    def discard(self) -> None:
        for coro in self.pending:
            coro.close()
        self.pending.clear()


SWEEP_PHOTO_TS = 1_760_000_000

SWEEP_BODY = {
    "params": {"search_text": "torrentshell", "order": "newest_first"},
    "tld": "fr",
    "keywords": ["torrentshell"],
    "visual_signature": "a boxy waterproof shell with a stowaway hood",
    "labels": {"brand": "Patagonia"},
}


class StubTriage:
    """The photo check, answering yes to everything and counting nothing else."""

    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    async def judge(self, items: list[Any], target: Any) -> TriageBatch:
        self.batches.append([item.item_id for item in items])
        return TriageBatch(
            results=[
                TriageItem(id=item.item_id, matches_target=True, confidence=0.9, reason="the hood")
                for item in items
            ],
            usage=None,
        )


@pytest.fixture
def sweep_client(
    web_settings: Settings,
    db: Database,
    repo: Repo,
    transport: ScriptedTransport,
) -> Iterator[Callable[..., tuple[TestClient, CollectingLauncher]]]:
    """A signed-in dashboard wired for sweeps, with the launcher in the test's hands."""
    with contextlib.ExitStack() as stack:
        launchers: list[CollectingLauncher] = []

        def build(
            *, with_client: bool = True, with_triage: bool = True, settings: Settings | None = None
        ) -> tuple[TestClient, CollectingLauncher]:
            sessions = SessionManager(db, transport)
            launcher = CollectingLauncher()
            launchers.append(launcher)
            stack.callback(launcher.discard)
            app = create_app(
                settings or web_settings,
                repo,
                None,
                None,
                client=VintedClient(transport, sessions) if with_client else None,
                sessions=sessions,
                triage=StubTriage() if with_triage else None,  # type: ignore[arg-type]
                verdict=None,
                launch=launcher,
            )
            test_client = stack.enter_context(TestClient(app))
            test_client.cookies.set(SESSION_COOKIE, TOKEN)
            return test_client, launcher

        yield build


def _queue_one_short_page(
    transport: ScriptedTransport, make_item: Callable[..., dict[str, Any]], count: int = 4
) -> None:
    """One page shorter than a full one, so the sweep reads it and stops."""
    transport.queue_catalog(
        [
            make_item(item_id, photo_ts=SWEEP_PHOTO_TS, title="Patagonia Torrentshell")
            for item_id in range(count)
        ]
    )


def test_launching_a_sweep_needs_a_login(client: TestClient) -> None:
    response = client.post("/api/magic-search/sweep", json=SWEEP_BODY)

    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["client", "triage"])
def test_without_a_vinted_client_or_a_photo_check_a_sweep_says_it_is_not_set_up(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]], missing: str
) -> None:
    """Half a Magic Search is not a sweep: both the reader and the judge have to be there."""
    test_client, launcher = sweep_client(
        with_client=missing != "client", with_triage=missing != "triage"
    )

    response = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)

    assert response.status_code == 503
    assert "not set up" in response.json()["detail"]
    assert launcher.pending == []  # nothing was launched, so nothing will be billed


def test_a_sweep_on_an_unknown_site_is_refused_before_anything_is_opened(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]], repo: Repo
) -> None:
    test_client, launcher = sweep_client()

    response = test_client.post("/api/magic-search/sweep", json={**SWEEP_BODY, "tld": "xx"})

    assert response.status_code == 404
    assert launcher.pending == []


async def test_a_sweep_answers_with_its_id_before_it_has_read_anything(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """The point of the endpoint: an id to poll, handed over while the run is still open."""
    test_client, launcher = sweep_client()
    _queue_one_short_page(transport, make_item)

    response = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)

    assert response.status_code == 202
    sweep_id = response.json()["sweep_id"]
    assert isinstance(sweep_id, int)
    assert transport.requests == []  # answered before a single page was fetched

    running = test_client.get(f"/api/sweeps/{sweep_id}").json()
    assert running["status"] == "running"
    assert running["candidates"] == []

    await launcher.drain()

    finished = test_client.get(f"/api/sweeps/{sweep_id}").json()
    assert finished["id"] == sweep_id
    assert finished["status"] == "ok"
    assert finished["pages_fetched"] == 1
    assert finished["kept"] == 4
    assert finished["triaged"] == 4
    assert len(finished["candidates"]) == 4


async def test_a_second_sweep_while_one_is_running_is_refused_not_billed(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """A double click must not mean a double bill, and the refusal says so in words."""
    test_client, launcher = sweep_client()
    _queue_one_short_page(transport, make_item)

    first = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)
    second = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)

    assert first.status_code == 202
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    assert len(launcher.pending) == 1

    # And the flag clears when the sweep ends, so the endpoint is usable again afterwards.
    await launcher.drain()
    _queue_one_short_page(transport, make_item)
    third = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)
    assert third.status_code == 202
    assert third.json()["sweep_id"] != first.json()["sweep_id"]


async def test_a_sweep_that_found_nothing_still_closes_and_frees_the_endpoint(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
) -> None:
    """Nothing queued, so the site answers with an empty page. Zero finds is not an error."""
    test_client, launcher = sweep_client()

    first = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)
    await launcher.drain()

    assert first.status_code == 202
    closed = test_client.get(f"/api/sweeps/{first.json()['sweep_id']}").json()
    assert closed["status"] == "ok"
    assert closed["kept"] == 0
    assert test_client.post("/api/magic-search/sweep", json=SWEEP_BODY).status_code == 202


async def test_a_sweep_that_crashes_is_closed_with_the_reason_not_left_running(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing the browser cannot recover from is a run that never stops saying
    `running`, so the detached task closes the row itself rather than dying quietly.

    `judge_sweep()` is documented not to raise; this pins what happens when something it
    did not anticipate does. The flag has to clear too — a crash that wedges the endpoint
    at 409 for the life of the process would be the worse half of the same bug.
    """

    async def explode(**_: Any) -> None:
        raise RuntimeError("the database went away mid-sweep")

    monkeypatch.setattr(sweep, "judge_sweep", explode)
    test_client, launcher = sweep_client()

    first = test_client.post("/api/magic-search/sweep", json=SWEEP_BODY)
    await launcher.drain()

    closed = test_client.get(f"/api/sweeps/{first.json()['sweep_id']}").json()
    assert closed["status"] == "partial"
    assert "the database went away mid-sweep" in closed["error"]
    assert closed["finished_at"] is not None

    monkeypatch.undo()
    assert test_client.post("/api/magic-search/sweep", json=SWEEP_BODY).status_code == 202


async def test_a_posted_ceiling_above_the_settings_is_clamped_down_to_them(
    web_settings: Settings,
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """A client-supplied ceiling is what a sweep spends money against, so it never grows."""
    settings = web_settings.model_copy(update={"sweep_max_pages": 1, "sweep_max_items": 2})
    test_client, launcher = sweep_client(settings=settings)
    # Two full pages queued. Only the first is allowed to be read, even though 9 was asked
    # for, and only two of its listings are allowed through the funnel.
    transport.queue_catalog(
        [
            make_item(item_id, photo_ts=SWEEP_PHOTO_TS, title="Patagonia Torrentshell")
            for item_id in range(PER_PAGE)
        ]
    )
    transport.queue_catalog(
        [
            make_item(item_id, photo_ts=SWEEP_PHOTO_TS, title="Patagonia Torrentshell")
            for item_id in range(PER_PAGE, 2 * PER_PAGE)
        ]
    )

    response = test_client.post(
        "/api/magic-search/sweep", json={**SWEEP_BODY, "max_pages": 9, "max_items": 999}
    )
    await launcher.drain()

    assert response.status_code == 202
    run = test_client.get(f"/api/sweeps/{response.json()['sweep_id']}").json()
    assert run["pages_fetched"] == 1
    assert run["kept"] == 2


async def test_a_ceiling_below_the_settings_is_honoured_as_asked(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
    transport: ScriptedTransport,
    make_item: Callable[..., dict[str, Any]],
) -> None:
    """Clamping is downward-only, so a smaller request is still a smaller sweep."""
    test_client, launcher = sweep_client()
    _queue_one_short_page(transport, make_item, count=5)

    response = test_client.post("/api/magic-search/sweep", json={**SWEEP_BODY, "max_items": 2})
    await launcher.drain()

    run = test_client.get(f"/api/sweeps/{response.json()['sweep_id']}").json()
    assert run["kept"] == 2


def test_a_sweep_without_the_params_it_needs_is_refused_by_shape(
    sweep_client: Callable[..., tuple[TestClient, CollectingLauncher]],
) -> None:
    test_client, launcher = sweep_client()

    response = test_client.post("/api/magic-search/sweep", json={"tld": "fr"})

    assert response.status_code == 422
    assert launcher.pending == []


# --- Turning a sweep into a standing watch ---------------------------------------------
#
# The last step of the Magic Search story. What matters is that the row it leaves behind is
# an ordinary watch — same canonical URL, same params — and that the title rules land as
# gates on that watch rather than as filters on the search.


WATCH_BODY = {
    "params": {
        "catalog_ids": "1206",
        "brand_ids": "7",
        "size_ids": "207,208",
        "price_to": "120",
        "search_text": "torrentshell",
        "order": "newest_first",
    },
    "tld": "sk",
    "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": "(?i)torrentshell"},
}


def test_creating_a_watch_needs_a_login(client: TestClient) -> None:
    response = client.post("/api/magic-search/watch", json=WATCH_BODY)

    assert response.status_code == 401


async def test_a_sweep_becomes_a_watch_with_its_title_rules_as_gates_not_filters(
    signed_in: TestClient, repo: Repo
) -> None:
    """R003: `watch_hints` gate the saved watch and never reach its search parameters."""
    sweep_id = await repo.create_sweep_run(
        tld="sk", params=dict(WATCH_BODY["params"]), keywords=["torrentshell"]
    )

    response = signed_in.post("/api/magic-search/watch", json={**WATCH_BODY, "sweep_id": sweep_id})

    assert response.status_code == 201
    query = await repo.get_query(response.json()["query_id"])
    assert query is not None
    assert query.required_keywords == ["torrentshell"]
    assert query.title_pattern == "(?i)torrentshell"
    assert "required_keywords" not in query.params
    assert "title_pattern" not in query.params
    # The params are exactly what was swept — no hint arrived under any name.
    assert query.params == WATCH_BODY["params"]
    assert "(?i)torrentshell" not in json.dumps(query.params)


async def test_a_watch_made_from_a_sweep_is_the_same_row_a_paste_would_make(
    signed_in: TestClient, repo: Repo
) -> None:
    """'Indistinguishable in the queries table' — so a paste of it is refused as a dupe."""
    created = signed_in.post("/api/magic-search/watch", json=WATCH_BODY)
    query = await repo.get_query(created.json()["query_id"])
    assert query is not None

    pasted = signed_in.post("/searches", data={"url": query.url}, follow_redirects=False)

    assert "already+being+watched" in pasted.headers["location"].replace("%20", "+")
    # The URL is canonical on its own terms and its params agree with it, which is what
    # makes the collision above possible at all.
    assert query.url == urls.normalise_search_url(query.url)
    assert urls.parse_search_params(query.url) == query.params
    assert query.params["order"] == "newest_first"


async def test_the_sweep_row_learns_which_watch_it_became(
    signed_in: TestClient, repo: Repo
) -> None:
    sweep_id = await repo.create_sweep_run(tld="sk", params={"catalog_ids": "1206"}, keywords=[])
    assert (await repo.get_sweep_run(sweep_id)) is not None
    assert (await repo.get_sweep_run(sweep_id)).query_id is None  # type: ignore[union-attr]

    response = signed_in.post("/api/magic-search/watch", json={**WATCH_BODY, "sweep_id": sweep_id})

    run = await repo.get_sweep_run(sweep_id)
    assert run is not None
    assert run.query_id == response.json()["query_id"]


async def test_a_watch_can_be_created_without_a_sweep_behind_it(
    signed_in: TestClient, repo: Repo
) -> None:
    """Promoting straight off the confirmation screen, before anyone paid for a sweep."""
    body = {k: v for k, v in WATCH_BODY.items() if k != "watch_hints"}

    response = signed_in.post("/api/magic-search/watch", json=body)

    assert response.status_code == 201
    query = await repo.get_query(response.json()["query_id"])
    assert query is not None
    assert query.required_keywords == []
    assert query.title_pattern is None


async def test_a_blank_name_is_filled_in_from_what_was_searched_for(
    signed_in: TestClient, repo: Repo
) -> None:
    response = signed_in.post("/api/magic-search/watch", json=WATCH_BODY)

    query = await repo.get_query(response.json()["query_id"])
    assert query is not None
    assert query.name == "torrentshell (sk)"


async def test_watching_the_same_search_twice_is_refused_in_plain_words(
    signed_in: TestClient, repo: Repo
) -> None:
    first = signed_in.post("/api/magic-search/watch", json=WATCH_BODY)
    assert first.status_code == 201

    second = signed_in.post("/api/magic-search/watch", json=WATCH_BODY)

    assert second.status_code == 409
    assert second.json()["error"] == "that search is already being watched"
    assert len(await repo.list_queries()) == 1


async def test_a_title_pattern_that_does_not_compile_is_refused_with_the_reason(
    signed_in: TestClient, repo: Repo
) -> None:
    response = signed_in.post(
        "/api/magic-search/watch",
        json={**WATCH_BODY, "watch_hints": {"required_keywords": [], "title_pattern": "(unclosed"}},
    )

    assert response.status_code == 422
    assert "does not compile" in response.json()["error"]
    assert await repo.list_queries() == []


async def test_params_with_no_filter_on_them_are_refused_before_anything_is_saved(
    signed_in: TestClient, repo: Repo
) -> None:
    response = signed_in.post(
        "/api/magic-search/watch", json={**WATCH_BODY, "params": {"order": "newest_first"}}
    )

    assert response.status_code == 422
    assert "no search filters" in response.json()["error"]
    assert await repo.list_queries() == []


def test_watching_on_an_unknown_country_site_is_refused(signed_in: TestClient) -> None:
    response = signed_in.post("/api/magic-search/watch", json={**WATCH_BODY, "tld": "xx"})

    assert response.status_code == 404
