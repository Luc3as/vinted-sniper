"""Everything you configured, as one JSON document — and the way back.

The database also holds listings, outbox rows and sessions, none of which you would want
to carry to another machine. What you would want is the part you typed: searches with
their filters, destinations with their settings, and which search feeds which
destination. That is what goes out, and what comes back in — additively, by URL and by
destination name, so importing the same file twice changes nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from vinted_sniper.db.repo import Repo

FORMAT_VERSION = 1


async def export_config(repo: Repo) -> dict[str, Any]:
    destinations = await repo.list_destinations()
    names_by_id = {d.id: d.name for d in destinations}
    searches = []
    for query in await repo.list_queries():
        routed = await repo.destination_ids_for_query(query.id)
        searches.append(
            {
                "name": query.name,
                "url": query.url,
                "poll_interval_s": query.poll_interval_s,
                "paused": query.paused,
                "banned_keywords": query.banned_keywords,
                "max_total_price": (
                    str(query.max_total_price) if query.max_total_price is not None else None
                ),
                "required_keywords": query.required_keywords,
                "title_pattern": query.title_pattern,
                "min_seller_rating": query.min_seller_rating,
                "min_seller_reviews": query.min_seller_reviews,
                "blocked_sellers": query.blocked_sellers,
                "destinations": sorted(names_by_id[i] for i in routed if i in names_by_id),
            }
        )
    return {
        "format": FORMAT_VERSION,
        "destinations": [
            {
                "kind": d.kind,
                "name": d.name,
                "config": d.config,
                "notify_status": d.notify_status,
                "quiet_hours": d.quiet_hours,
            }
            for d in destinations
            if d.active
        ],
        "searches": searches,
    }


async def import_config(repo: Repo, document: dict[str, Any]) -> dict[str, int]:
    """Merge a document produced by export_config. Returns what was added."""
    if document.get("format") != FORMAT_VERSION:
        raise ValueError(f"unsupported backup format {document.get('format')!r}")

    added = {"destinations": 0, "searches": 0, "routes": 0}
    existing = {d.name: d.id for d in await repo.list_destinations()}

    for entry in document.get("destinations", []):
        name = str(entry["name"])
        if name in existing:
            continue
        existing[name] = await repo.add_destination(
            kind=str(entry["kind"]),
            name=name,
            config=dict(entry.get("config") or {}),
            notify_status=bool(entry.get("notify_status", False)),
            quiet_hours=entry.get("quiet_hours"),
        )
        added["destinations"] += 1

    for entry in document.get("searches", []):
        url = str(entry["url"])
        query = await repo.find_query_by_url(url)
        if query is None:
            from vinted_sniper.vinted import urls  # noqa: PLC0415 - avoids an import cycle

            normalised = urls.normalise_search_url(url)
            price = entry.get("max_total_price")
            query_id = await repo.add_query(
                name=str(entry.get("name") or urls.extract_tld(normalised)),
                url=normalised,
                tld=urls.extract_tld(normalised),
                params=urls.parse_search_params(normalised),
                poll_interval_s=int(entry.get("poll_interval_s") or 60),
                banned_keywords=list(entry.get("banned_keywords") or []),
                max_total_price=Decimal(str(price)) if price is not None else None,
                required_keywords=list(entry.get("required_keywords") or []),
                title_pattern=entry.get("title_pattern"),
                min_seller_rating=entry.get("min_seller_rating"),
                min_seller_reviews=entry.get("min_seller_reviews"),
                blocked_sellers=list(entry.get("blocked_sellers") or []),
            )
            if entry.get("paused"):
                await repo.set_paused(query_id, True)
            added["searches"] += 1
        else:
            query_id = query.id
        routed = set(await repo.destination_ids_for_query(query_id))
        for name in entry.get("destinations", []):
            destination_id = existing.get(str(name))
            if destination_id is not None and destination_id not in routed:
                await repo.route(query_id, destination_id)
                added["routes"] += 1
    return added
