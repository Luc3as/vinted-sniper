"""Export what was typed, import it back, and nothing doubles."""

from __future__ import annotations

from decimal import Decimal

from vinted_sniper import backup
from vinted_sniper.db.repo import Repo


async def test_a_round_trip_reproduces_the_configuration(repo: Repo) -> None:
    phone = await repo.add_destination(
        kind="ntfy",
        name="phone",
        config={"topic": "t"},
        notify_status=True,
        quiet_hours="23:00-07:00",
    )
    query_id = await repo.add_query(
        name="Rab Downpour",
        url="https://www.vinted.sk/catalog?search_text=rab%20downpour&price_to=60",
        tld="sk",
        params={"search_text": "rab downpour", "price_to": "60"},
        poll_interval_s=300,
        banned_keywords=["replika"],
        max_total_price=Decimal("55"),
        required_keywords=["downpour"],
        min_seller_rating=0.9,
        blocked_sellers=["scammer"],
    )
    await repo.route(query_id, phone)

    document = await backup.export_config(repo)
    assert document["format"] == 1
    assert document["destinations"][0]["quiet_hours"] == "23:00-07:00"
    assert document["searches"][0]["destinations"] == ["phone"]
    assert Decimal(document["searches"][0]["max_total_price"]) == Decimal("55")

    # Importing the same thing again is a no-op.
    assert await backup.import_config(repo, document) == {
        "destinations": 0,
        "searches": 0,
        "routes": 0,
    }

    # A fresh database gets everything back.
    await repo.delete_query(query_id)
    await repo.deactivate_destination(phone, "gone")
    document["destinations"][0]["name"] = "phone2"
    document["searches"][0]["destinations"] = ["phone2"]
    added = await backup.import_config(repo, document)
    assert added == {"destinations": 1, "searches": 1, "routes": 1}

    (restored,) = await repo.list_queries()
    assert restored.required_keywords == ["downpour"]
    assert restored.blocked_sellers == ["scammer"]
    assert restored.max_total_price == Decimal("55")
    assert restored.min_seller_rating == 0.9


async def test_an_unknown_format_is_refused(repo: Repo) -> None:
    try:
        await backup.import_config(repo, {"format": 99})
    except ValueError as exc:
        assert "format" in str(exc)
    else:
        raise AssertionError("should have refused")
