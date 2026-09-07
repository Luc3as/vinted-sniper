"""Rejecting ids Vinted does not have, before they become a search that finds nothing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import ScriptedTransport
from vinted_sniper.db import Database
from vinted_sniper.db.repo import Repo
from vinted_sniper.magic.errors import MappingError
from vinted_sniper.magic.models import MappedQuery
from vinted_sniper.magic.validate import find_catalog, validate
from vinted_sniper.vinted.errors import VintedError
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.taxonomy import Taxonomy
from vinted_sniper.vinted.transport import Response

# The tree as Vinted embeds it in the page: nested under "catalogs", with extra keys.
RAW_TREE: list[dict[str, Any]] = [
    {
        "id": 5,
        "title": "Men",
        "photo": {"url": "irrelevant"},
        "catalogs": [
            {
                "id": 2050,
                "title": "Men's clothing",
                "catalogs": [{"id": 2052, "title": "Jackets & Coats", "catalogs": []}],
            }
        ],
    },
    {"id": 1904, "title": "Women", "catalogs": []},
]

# The same tree after compaction — what Taxonomy caches and what find_catalog walks.
COMPACT_TREE: list[dict[str, Any]] = [
    {
        "id": 5,
        "title": "Men",
        "children": [
            {
                "id": 2050,
                "title": "Men's clothing",
                "children": [{"id": 2052, "title": "Jackets & Coats", "children": []}],
            }
        ],
    },
    {"id": 1904, "title": "Women", "children": []},
]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def taxonomy(db: Database, repo: Repo, transport: ScriptedTransport, clock: Clock) -> Taxonomy:
    return Taxonomy(SessionManager(db, transport), repo, clock=clock)


def flight_page(tree: list[dict[str, Any]] = RAW_TREE, csrf: str = "csrf-tok") -> str:
    """A search page the way Vinted serves it: the data inside an escaped JS string."""
    embedded = json.dumps(json.dumps({"CSRF_TOKEN": csrf, "catalogTree": tree}))
    return f"<html><script>self.__next_f.push([1,{embedded}])</script></html>"


def queue_page(transport: ScriptedTransport, html: str) -> None:
    transport.queue_root(
        Response(status_code=200, text=html, headers={}, cookies={"access_token_web": "t"})
    )


def queue_bootstrap(transport: ScriptedTransport) -> None:
    """The homepage fetch that mints the session, before any page mining."""
    queue_page(transport, "<html>the homepage</html>")


def queue_json(transport: ScriptedTransport, payload: dict[str, Any]) -> None:
    transport.queue(Response(status_code=200, text=json.dumps(payload), headers={}, cookies={}))


async def cache_tree(repo: Repo, clock: Clock, tld: str = "fr") -> None:
    """Put the compacted tree in the DB cache, as a previous run would have left it."""
    await repo.set_state_value(
        f"catalog_tree:{tld}", json.dumps({"fetched_at": int(clock.now), "tree": COMPACT_TREE})
    )


def mapping(**fields: Any) -> MappedQuery:
    return MappedQuery.model_validate(fields)


# --- The pure tree walk ---------------------------------------------------------------


def test_find_catalog_reaches_the_deepest_node() -> None:
    found = find_catalog(COMPACT_TREE, 2052)

    assert found is not None
    assert found["title"] == "Jackets & Coats"


def test_find_catalog_returns_none_for_an_id_that_is_not_there() -> None:
    assert find_catalog(COMPACT_TREE, 9999) is None
    assert find_catalog([], 5) is None


# --- The happy path -------------------------------------------------------------------


async def test_a_mapping_whose_ids_all_exist_passes(
    taxonomy: Taxonomy, transport: ScriptedTransport
) -> None:
    queue_bootstrap(transport)
    queue_page(transport, flight_page())
    queue_json(transport, {"options": [{"id": 90804, "title": "Patagonia"}]})
    queue_json(
        transport,
        {
            "options": [
                {"id": "MEN-TOPS", "options": [{"id": 208, "title": "M", "items_count": 12}]}
            ]
        },
    )

    await validate(
        mapping(
            catalog={"id": 2052, "name": "Jackets & Coats"},
            brand={"id": 90804, "name": "Patagonia"},
            sizes=[{"id": 208, "name": "M"}],
        ),
        tld="fr",
        taxonomy=taxonomy,
    )

    scoped = [r for r in transport.requests if "filters/search" in r["url"]]
    assert scoped[0]["params"]["catalog_ids"] == "2052", "the brand lookup is scoped to the catalog"


async def test_a_search_with_no_ids_at_all_asks_vinted_nothing(
    taxonomy: Taxonomy, transport: ScriptedTransport
) -> None:
    await validate(mapping(search_text="patagonia torrentshell"), tld="fr", taxonomy=taxonomy)

    assert transport.requests == []


# --- Tier 1: the catalog --------------------------------------------------------------


async def test_an_invented_catalog_id_is_rejected_by_id_and_by_name(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)

    with pytest.raises(MappingError) as caught:
        await validate(
            mapping(catalog={"id": 9999, "name": "panske bundy"}), tld="fr", taxonomy=taxonomy
        )

    message = str(caught.value)
    assert "9999" in message
    assert "panske bundy" in message


async def test_the_catalog_tier_costs_zero_requests_once_the_tree_is_cached(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)

    await validate(mapping(catalog={"id": 2052, "name": "Jackets"}), tld="fr", taxonomy=taxonomy)
    with pytest.raises(MappingError):
        await validate(
            mapping(catalog={"id": 4242, "name": "invented"}), tld="fr", taxonomy=taxonomy
        )

    assert transport.requests == [], "the cached tree answers both checks offline"


# --- Tier 2: the brand ----------------------------------------------------------------


async def test_an_invented_brand_id_is_rejected_with_what_vinted_did_find(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)
    queue_bootstrap(transport)
    queue_page(transport, flight_page())
    queue_json(transport, {"options": [{"id": 90804, "title": "Patagonia"}]})

    with pytest.raises(MappingError) as caught:
        await validate(
            mapping(
                catalog={"id": 2052, "name": "Jackets & Coats"},
                brand={"id": 12345, "name": "Patagoniaa"},
            ),
            tld="fr",
            taxonomy=taxonomy,
        )

    message = str(caught.value)
    assert "12345" in message
    assert "Patagoniaa" in message
    assert "Patagonia" in message, "the message should name the brand Vinted actually knows"


async def test_an_empty_scoped_brand_lookup_falls_back_to_the_global_endpoint(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)
    queue_bootstrap(transport)
    queue_page(transport, flight_page())
    queue_json(transport, {"options": []})
    queue_json(transport, {"brands": [{"id": 90804, "title": "Patagonia", "item_count": 4}]})

    await validate(
        mapping(
            catalog={"id": 2052, "name": "Jackets & Coats"},
            brand={"id": 90804, "name": "Patagonia"},
        ),
        tld="fr",
        taxonomy=taxonomy,
    )

    urls = [r["url"] for r in transport.requests]
    assert any("filters/search" in url for url in urls), "the scoped lookup ran first"
    assert any("/api/v2/brands" in url for url in urls), "the global one ran after it was empty"


# --- Tier 3: the sizes ----------------------------------------------------------------


async def test_every_invented_size_id_is_named_in_one_message(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)
    queue_bootstrap(transport)
    queue_page(transport, flight_page())
    queue_json(transport, {"options": [{"id": 208, "title": "M"}]})

    with pytest.raises(MappingError) as caught:
        await validate(
            mapping(
                catalog={"id": 2052, "name": "Jackets & Coats"},
                sizes=[{"id": 777, "name": "XS"}, {"id": 888, "name": "XXL"}],
            ),
            tld="fr",
            taxonomy=taxonomy,
        )

    message = str(caught.value)
    assert "777" in message and "XS" in message
    assert "888" in message and "XXL" in message


async def test_a_size_without_a_catalog_cannot_be_checked_and_says_so(
    taxonomy: Taxonomy, transport: ScriptedTransport
) -> None:
    with pytest.raises(MappingError, match="category"):
        await validate(mapping(sizes=[{"id": 208, "name": "M"}]), tld="fr", taxonomy=taxonomy)

    assert transport.requests == [], "no catalog means nothing worth asking Vinted"


# --- When the check itself cannot run -------------------------------------------------


async def test_a_failing_lookup_surfaces_as_a_mapping_error_not_a_vinted_error(
    taxonomy: Taxonomy, transport: ScriptedTransport, repo: Repo, clock: Clock
) -> None:
    await cache_tree(repo, clock)
    queue_bootstrap(transport)
    queue_page(transport, flight_page())
    transport.queue_status(503, "upstream is down")

    with pytest.raises(MappingError) as caught:
        await validate(
            mapping(
                catalog={"id": 2052, "name": "Jackets & Coats"},
                sizes=[{"id": 208, "name": "M"}],
            ),
            tld="fr",
            taxonomy=taxonomy,
        )

    assert not isinstance(caught.value, VintedError)
    assert "could not be checked" in str(caught.value)


async def test_an_unreadable_category_tree_is_a_mapping_error_too(
    taxonomy: Taxonomy, transport: ScriptedTransport
) -> None:
    queue_bootstrap(transport)
    transport.queue_root(Response(status_code=503, text="upstream is down", headers={}, cookies={}))

    with pytest.raises(MappingError, match="could not be checked"):
        await validate(
            mapping(catalog={"id": 2052, "name": "Jackets & Coats"}), tld="fr", taxonomy=taxonomy
        )
