"""The web UI.

On by default, bound to localhost unless you say otherwise. It exists
because editing a config file is where most people give up: pasting a Vinted URL into a box
is not.

It also answers the question the issue trackers of similar tools are full of — "why did it
stop?" — by showing, per search, when it last succeeded, what the last error was, and
whether the catalog has gone quiet. Nothing here is guessed from silence.

It listens on localhost and opens straight onto the dashboard — nothing to sign in to.
Setting VINTED_SNIPER_WEB_AUTH_TOKEN puts a password on it, which is the right move before
exposing it beyond your own machine: the database holds your webhook URLs and chat ids,
and an open dashboard on a public port would be a way to hand them out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from vinted_sniper import backup, i18n
from vinted_sniper.config import MIN_POLL_INTERVAL_S, Settings
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import filters, health, quiet
from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.log import get_logger
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.errors import VintedError
from vinted_sniper.vinted.taxonomy import FACET_CODES, Taxonomy
from vinted_sniper.web.security import (
    LoginThrottle,
    SameOriginMiddleware,
    SecurityHeadersMiddleware,
    client_address,
)

log = get_logger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "vinted_sniper_session"


def _authorised(supplied: str | None, expected: SecretStr | None) -> bool:
    if expected is None:
        return True
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected.get_secret_value())


def create_app(settings: Settings, repo: Repo, taxonomy: Taxonomy | None = None) -> FastAPI:
    token = settings.web_auth_token  # None means no password: the dashboard just opens

    app = FastAPI(title="vinted-sniper", docs_url=None, redoc_url=None)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SameOriginMiddleware)
    throttle = LoginThrottle()

    async def require_login(
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # A machine calling the API sends the same token as a bearer; a browser has
        # the cookie the login form set. Either will do.
        bearer = authorization.removeprefix("Bearer ").strip() if authorization else None
        if not (_authorised(session, token) or (bearer and _authorised(bearer, token))):
            raise HTTPException(status_code=401, detail="not signed in")

    guard = Depends(require_login)

    # --- Health, unauthenticated on purpose: the container check runs it ------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        alive = await health.is_alive(repo)
        return JSONResponse({"alive": alive}, status_code=200 if alive else 503)

    # --- Signing in ----------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if token is None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login(request: Request, access_token: Annotated[str, Form()]) -> Response:
        if token is None:
            return RedirectResponse("/", status_code=303)
        client = client_address(request)
        if (wait := throttle.retry_after(client)) > 0:
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": f"Too many wrong tokens. Try again in {int(wait) + 1} s."},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        if not _authorised(access_token, token):
            throttle.failed(client)
            log.warning("web.login_failed", client=client)
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "That token does not match."},
                status_code=401,
            )
        throttle.succeeded(client)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            access_token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=30 * 86_400,
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # --- Dashboard -----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def found_page(
        request: Request,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> Response:
        """The landing page: what the searches have found, newest first."""
        if not _authorised(session, token):
            return RedirectResponse("/login", status_code=303)

        snapshot = await health.snapshot(repo)
        return TEMPLATES.TemplateResponse(
            request,
            "found.html",
            {
                "nav": "found",
                "snapshot": snapshot,
                "auth_enabled": token is not None,
                "recent": _listing_views(await repo.recent_items(limit=60), now=int(time.time())),
            },
        )

    @app.get("/searches", response_class=HTMLResponse)
    async def searches_page(
        request: Request,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> Response:
        """The settings page: searches, destinations, export."""
        if not _authorised(session, token):
            return RedirectResponse("/login", status_code=303)

        snapshot = await health.snapshot(repo)
        destinations = await repo.list_destinations()
        queries = {query.id: query for query in await repo.list_queries()}
        watched_tlds = [search.tld for search in snapshot.searches]
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "nav": "searches",
                "snapshot": snapshot,
                "queries": queries,
                "destinations": destinations,
                "languages": i18n.LANGUAGES,
                "first_run_newest": settings.first_run_mode == "newest",
                "auth_enabled": token is not None,
                "now": int(time.time()),
                "min_interval": MIN_POLL_INTERVAL_S,
                "default_interval": settings.poll_default_interval_s,
                "builder_enabled": taxonomy is not None,
                "known_tlds": sorted(urls.KNOWN_TLDS),
                # Open the builder on the site the user already watches most.
                "default_tld": (
                    Counter(watched_tlds).most_common(1)[0][0] if watched_tlds else "fr"
                ),
            },
        )

    @app.post("/api/items/{item_id}/enrichment")
    async def post_enrichment(item_id: int, verdict: EnrichmentIn, _: None = guard) -> JSONResponse:
        """What an outside agent concluded about a listing. Releases any held alert."""
        if not await repo.store_enrichment(
            item_id, verdict, followup_min_score=settings.enrichment_highlight_score
        ):
            raise HTTPException(status_code=404, detail="no such listing")
        log.info(
            "enrichment.received",
            item_id=item_id,
            score=verdict.score,
            matches_query=verdict.matches_query,
        )
        return JSONResponse({"ok": True})

    @app.get("/api/export")
    async def api_export(_: None = guard) -> JSONResponse:
        """Searches, destinations and routes — the part you typed — as one JSON document."""
        return JSONResponse(await backup.export_config(repo))

    @app.post("/api/import")
    async def api_import(document: dict[str, Any], _: None = guard) -> JSONResponse:
        try:
            added = await backup.import_config(repo, document)
        except (ValueError, KeyError, urls.InvalidSearchURLError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "added": added})

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(
        request: Request,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        search: str | None = None,
        status: str | None = None,
    ) -> Response:
        if not _authorised(session, token):
            return RedirectResponse("/login", status_code=303)
        # The filter form submits search= (empty) for "all"; an int-typed parameter
        # would reject that outright with a bare 422, so parse leniently instead.
        query_id = _int_or_none(search or "")
        rows = await repo.delivery_history(limit=200, query_id=query_id, status=status or None)
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "nav": "history",
                "rows": _history_views(rows, now=int(time.time())),
                "queries": await repo.list_queries(),
                "selected_search": query_id,
                "selected_status": status or "",
                "auth_enabled": token is not None,
            },
        )

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(
        request: Request,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> Response:
        if not _authorised(session, token):
            return RedirectResponse("/login", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "help.html",
            {
                "nav": "help",
                "auth_enabled": token is not None,
                "settings_view": {
                    "poll_default_interval_s": settings.poll_default_interval_s,
                    "site_requests_per_minute": settings.site_requests_per_minute,
                    "price_drop_min_percent": settings.price_drop_min_percent,
                    "enrichment_wait_s": settings.enrichment_wait_s,
                    "enrichment_highlight_score": settings.enrichment_highlight_score,
                    "enrichment_silent_below": settings.enrichment_silent_below,
                    "timezone": settings.timezone,
                    "weekly_report": settings.weekly_report,
                    "outbox_expiry_minutes": settings.outbox_expiry_minutes,
                    "item_retention_days": settings.item_retention_days,
                    "telegram": settings.telegram_bot_token is not None,
                },
            },
        )

    @app.get("/api/history")
    async def api_history(
        search: str | None = None, status: str | None = None, _: None = guard
    ) -> JSONResponse:
        rows = await repo.delivery_history(
            limit=200, query_id=_int_or_none(search or ""), status=status or None
        )
        return JSONResponse({"deliveries": _history_views(rows, now=int(time.time()))})

    @app.get("/api/health")
    async def api_health(_: None = guard) -> JSONResponse:
        snapshot = await health.snapshot(repo)
        return JSONResponse(snapshot.as_dict())

    # --- Searches ------------------------------------------------------------------

    @app.post("/searches")
    async def add_search(
        url: Annotated[str, Form()],
        name: Annotated[str, Form()] = "",
        interval: Annotated[str, Form()] = "",
        max_total_price: Annotated[str, Form()] = "",
        banned_keywords: Annotated[str, Form()] = "",
        required_keywords: Annotated[str, Form()] = "",
        title_pattern: Annotated[str, Form()] = "",
        min_seller_rating: Annotated[str, Form()] = "",
        min_seller_reviews: Annotated[str, Form()] = "",
        blocked_sellers: Annotated[str, Form()] = "",
        max_market_percentile: Annotated[str, Form()] = "",
        destination_ids: Annotated[list[int] | None, Form()] = None,
        _: None = guard,
    ) -> Response:
        try:
            normalised = urls.normalise_search_url(url)
            tld = urls.extract_tld(normalised)
            params = urls.parse_search_params(normalised)
        except urls.InvalidSearchURLError as exc:
            return _redirect_with_error(str(exc))

        if await repo.find_query_by_url(normalised) is not None:
            return _redirect_with_error("that search is already being watched")

        title_pattern = title_pattern.strip()
        if title_pattern and (problem := filters.validate_pattern(title_pattern)):
            return _redirect_with_error(f"title pattern does not compile: {problem}")
        rating = _rating_or_none(min_seller_rating)
        if min_seller_rating.strip() and rating is None:
            return _redirect_with_error("minimum seller rating must be between 0 and 100")

        query_id = await repo.add_query(
            name=name.strip() or _name_from(params, tld),
            url=normalised,
            tld=tld,
            params=params,
            poll_interval_s=max(
                _int_or_none(interval) or settings.poll_default_interval_s, MIN_POLL_INTERVAL_S
            ),
            banned_keywords=_csv(banned_keywords),
            max_total_price=_decimal_or_none(max_total_price),
            required_keywords=_csv(required_keywords),
            title_pattern=title_pattern or None,
            min_seller_rating=rating,
            min_seller_reviews=_int_or_none(min_seller_reviews),
            blocked_sellers=_csv(blocked_sellers),
            max_market_percentile=_percent_or_none(max_market_percentile),
        )
        for destination_id in destination_ids or []:
            await repo.route(query_id, destination_id)
        return _redirect_with_notice(
            "Search added. Its first check records what is already listed without alerting."
        )

    @app.post("/searches/{query_id}/clone")
    async def clone_search(query_id: int, tld: Annotated[str, Form()], _: None = guard) -> Response:
        """The same search on another country's site. Vinted's filter ids are shared across
        its sites, so the URL only needs a new domain; the routing comes along."""
        source = await repo.get_query(query_id)
        if source is None:
            return _redirect_with_error("that search no longer exists")
        tld = tld.strip().lower()
        if tld not in urls.KNOWN_TLDS:
            return _redirect_with_error(f"vinted.{tld} is not a site I know")
        if tld == source.tld:
            return _redirect_with_error(f"that search already runs on vinted.{tld}")
        target = urls.normalise_search_url(urls.swap_tld(source.url, tld))
        if await repo.find_query_by_url(target) is not None:
            return _redirect_with_error(f"this search already exists on vinted.{tld}")
        base_name = source.name.removesuffix(f" ({source.tld})")
        new_id = await repo.clone_query(query_id, url=target, tld=tld, name=f"{base_name} ({tld})")
        if new_id is None:
            return _redirect_with_error("that search no longer exists")
        return _redirect_with_notice(
            f"Cloned to vinted.{tld} with the same filters and destinations."
        )

    @app.post("/searches/{query_id}/edit")
    async def edit_search(
        query_id: int,
        name: Annotated[str, Form()] = "",
        interval: Annotated[str, Form()] = "",
        max_total_price: Annotated[str, Form()] = "",
        banned_keywords: Annotated[str, Form()] = "",
        required_keywords: Annotated[str, Form()] = "",
        title_pattern: Annotated[str, Form()] = "",
        min_seller_rating: Annotated[str, Form()] = "",
        min_seller_reviews: Annotated[str, Form()] = "",
        blocked_sellers: Annotated[str, Form()] = "",
        max_market_percentile: Annotated[str, Form()] = "",
        _: None = guard,
    ) -> Response:
        query = await repo.get_query(query_id)
        if query is None:
            return _redirect_with_error("that search no longer exists")
        title_pattern = title_pattern.strip()
        if title_pattern and (problem := filters.validate_pattern(title_pattern)):
            return _redirect_with_error(f"title pattern does not compile: {problem}")
        rating = _rating_or_none(min_seller_rating)
        if min_seller_rating.strip() and rating is None:
            return _redirect_with_error("minimum seller rating must be between 0 and 100")

        await repo.update_query(
            query_id,
            name=name.strip() or query.name,
            poll_interval_s=max(
                _int_or_none(interval) or query.poll_interval_s, MIN_POLL_INTERVAL_S
            ),
            banned_keywords=_csv(banned_keywords),
            max_total_price=_decimal_or_none(max_total_price),
            required_keywords=_csv(required_keywords),
            title_pattern=title_pattern or None,
            min_seller_rating=rating,
            min_seller_reviews=_int_or_none(min_seller_reviews),
            blocked_sellers=_csv(blocked_sellers),
            max_market_percentile=_percent_or_none(max_market_percentile),
        )
        return _redirect_with_notice("Saved. The new settings apply from the next check.")

    @app.post("/searches/interval")
    async def set_every_interval(interval: Annotated[str, Form()] = "", _: None = guard) -> Response:
        value = _int_or_none(interval)
        if value is None:
            return _redirect_with_error("the check interval must be a number of seconds")
        await repo.set_all_intervals(max(value, MIN_POLL_INTERVAL_S))
        return RedirectResponse("/searches", status_code=303)

    @app.post("/searches/{query_id}/pause")
    async def pause_search(
        query_id: int, paused: Annotated[str, Form()], _: None = guard
    ) -> Response:
        await repo.set_paused(query_id, paused == "1")
        return RedirectResponse("/searches", status_code=303)

    @app.post("/searches/{query_id}/delete")
    async def delete_search(query_id: int, _: None = guard) -> Response:
        await repo.delete_query(query_id)
        return RedirectResponse("/searches", status_code=303)

    # --- Filter data for the advanced search builder -------------------------------
    # Thin JSON pass-throughs the dashboard's picker calls. The taxonomy service does
    # the talking to Vinted; a missing service (bare create_app in tests, or the web UI
    # run without the engine) answers 503 rather than pretending the picker can work.

    def _taxonomy_or_503() -> Taxonomy:
        if taxonomy is None:
            raise HTTPException(status_code=503, detail="the search builder is not available")
        return taxonomy

    def _known_tld(tld: str) -> str:
        if tld not in urls.KNOWN_TLDS:
            raise HTTPException(status_code=404, detail=f"vinted.{tld} is not a known site")
        return tld

    @app.get("/api/filters/{tld}/categories")
    async def filter_categories(tld: str, _: None = guard) -> JSONResponse:
        service = _taxonomy_or_503()
        try:
            tree = await service.categories(_known_tld(tld))
        except VintedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"categories": tree})

    @app.get("/api/filters/{tld}/brands")
    async def filter_brands(
        tld: str, q: str = "", catalog_ids: str = "", _: None = guard
    ) -> JSONResponse:
        service = _taxonomy_or_503()
        if len(q.strip()) < 2:  # noqa: PLR2004 - an autocomplete needs two letters
            return JSONResponse({"brands": []})
        try:
            brands = await service.brands(_known_tld(tld), q, _id_list(catalog_ids))
        except VintedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"brands": brands})

    @app.get("/api/filters/{tld}/facets/{code}")
    async def filter_facet(
        tld: str, code: str, catalog_ids: str = "", _: None = guard
    ) -> JSONResponse:
        service = _taxonomy_or_503()
        if code not in FACET_CODES:
            raise HTTPException(status_code=404, detail=f"no filter called {code!r}")
        try:
            options = await service.facet_options(_known_tld(tld), code, _id_list(catalog_ids))
        except VintedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"options": options})

    # --- Destinations --------------------------------------------------------------

    @app.post("/destinations")
    async def add_destination(
        kind: Annotated[str, Form()],
        name: Annotated[str, Form()] = "",
        target: Annotated[str, Form()] = "",
        quiet_hours: Annotated[str, Form()] = "",
        notify_status: Annotated[str, Form()] = "",
        language: Annotated[str, Form()] = "en",
        _: None = guard,
    ) -> Response:
        target = target.strip()
        quiet_hours = quiet_hours.strip()
        if quiet_hours and (problem := quiet.validate(quiet_hours)):
            return _redirect_with_error(problem)
        config = _destination_config(kind, target)
        if isinstance(config, str):
            return _redirect_with_error(config)

        await repo.add_destination(
            kind=kind,
            name=name.strip() or kind,
            config=config,
            notify_status=notify_status == "1",
            quiet_hours=quiet_hours or None,
            language=language,
        )
        return _redirect_with_notice("Destination added.")

    @app.post("/destinations/{destination_id}/language")
    async def set_destination_language(
        destination_id: int, language: Annotated[str, Form()], _: None = guard
    ) -> Response:
        await repo.set_language(destination_id, language)
        return _redirect_with_notice(
            f"Alerts to this destination will be in {i18n.LANGUAGES[i18n.normalise(language)]}."
        )

    @app.post("/destinations/{destination_id}/quiet")
    async def set_destination_quiet(
        destination_id: int, quiet_hours: Annotated[str, Form()] = "", _: None = guard
    ) -> Response:
        quiet_hours = quiet_hours.strip()
        if quiet_hours and (problem := quiet.validate(quiet_hours)):
            return _redirect_with_error(problem)
        await repo.set_quiet_hours(destination_id, quiet_hours or None)
        return RedirectResponse("/searches", status_code=303)

    @app.post("/destinations/{destination_id}/status")
    async def set_destination_status(
        destination_id: int, enabled: Annotated[str, Form()], _: None = guard
    ) -> Response:
        await repo.set_notify_status(destination_id, enabled == "1")
        return RedirectResponse("/searches", status_code=303)

    @app.post("/destinations/{destination_id}/edit")
    async def edit_destination(
        destination_id: int,
        name: Annotated[str, Form()] = "",
        target: Annotated[str, Form()] = "",
        _: None = guard,
    ) -> Response:
        destination = await repo.get_destination(destination_id)
        if destination is None:
            return _redirect_with_error("that destination no longer exists")
        config = _destination_config(destination.kind, target.strip())
        if isinstance(config, str):
            return _redirect_with_error(config)
        await repo.update_destination(
            destination_id, name=name.strip() or destination.name, config=config
        )
        return _redirect_with_notice("Destination saved.")

    @app.post("/destinations/{destination_id}/enable")
    async def enable_destination(destination_id: int, _: None = guard) -> Response:
        await repo.reactivate_destination(destination_id)
        return _redirect_with_notice("Destination enabled again. Delivery resumes.")

    @app.post("/destinations/{destination_id}/delete")
    async def delete_destination(destination_id: int, _: None = guard) -> Response:
        await repo.delete_destination(destination_id)
        return _redirect_with_notice("Destination deleted.")

    @app.post("/searches/{query_id}/routes")
    async def set_routes(
        query_id: int,
        destination_ids: Annotated[list[int] | None, Form()] = None,
        _: None = guard,
    ) -> Response:
        wanted = set(destination_ids or [])
        current = set(await repo.destination_ids_for_query(query_id))
        for destination_id in current - wanted:
            await repo.unroute(query_id, destination_id)
        for destination_id in wanted - current:
            await repo.route(query_id, destination_id)
        return RedirectResponse("/searches", status_code=303)

    # --- RSS -----------------------------------------------------------------------

    @app.get("/rss/{query_id}.xml")
    async def rss(query_id: int, key: str = "") -> Response:
        # Feed readers cannot log in, so the token travels in the query string here.
        if not _authorised(key, token):
            raise HTTPException(status_code=401, detail="add ?key=<your token>")
        query = await repo.get_query(query_id)
        if query is None:
            raise HTTPException(status_code=404, detail="no such search")
        rows = [row for row in await repo.recent_items(limit=100) if row["query_id"] == query_id]
        return Response(content=_rss_feed(query.name, rows), media_type="application/rss+xml")

    return app


def _redirect_with_error(message: str) -> RedirectResponse:
    return RedirectResponse(f"/searches?error={quote(message)}", status_code=303)


def _destination_config(kind: str, target: str) -> dict[str, Any] | str:
    """The config dict a destination of this kind stores, or a human-readable complaint."""
    match kind:
        case "discord":
            if not target.startswith("https://"):
                return "paste the full Discord webhook URL"
            return {"webhook_url": target}
        case "telegram":
            if not target:
                return "add the chat id, or use the pairing link from the command line"
            return {"chat_id": target}
        case "webhook":
            return {"url": target}
        case "ntfy":
            return {"topic": target}
        case _:
            return f"unknown destination type {kind!r}"


def _listing_views(rows: list[Any], now: int) -> list[dict[str, Any]]:
    """Rows from the items table as the listing cards want them.

    Photos and formatting are settled here so the template stays declarative, and a row
    from before the gallery migration degrades to its cover photo rather than an error.
    """
    views: list[dict[str, Any]] = []
    for row in rows:
        photos: list[str] = []
        if row["photo_urls_json"]:
            with contextlib.suppress(ValueError):
                photos = [str(url) for url in json.loads(row["photo_urls_json"])]
        if not photos and row["photo_url"]:
            photos = [row["photo_url"]]

        currency = row["currency"] or ""
        price = row["price"]
        total = row["total_price"]
        # The same 0..1-to-stars reading the notifications use.
        stars = round(row["seller_rating"] * 50) / 10 if row["seller_rating"] is not None else None
        views.append(
            {
                "title": row["title"] or f"Listing {row['item_id']}",
                "url": row["url"],
                "photos": photos,
                "price": f"{price:.2f} {currency}".strip() if price is not None else None,
                "total_price": (
                    f"{total:.2f} {currency}".strip()
                    if total is not None and total != price
                    else None
                ),
                "brand": row["brand"],
                "size": row["size"],
                "condition": row["condition"],
                "seller_login": row["seller_login"],
                "seller_url": (
                    urls.member_url(row["tld"], row["seller_id"]) if row["seller_id"] else None
                ),
                "seller_stars": f"{stars:.1f}" if stars is not None else None,
                "seller_feedback_count": row["seller_feedback_count"],
                "favourite_count": row["favourite_count"] or 0,
                "price_dropped": bool(row["price_changed_at"]),
                "deal_score": row["enrich_score"] if row["enriched_at"] else None,
                "verdict": row["enrich_verdict"] if row["enriched_at"] else None,
                "query_name": row["query_name"],
                "age": _age(now - row["first_seen_at"]),
            }
        )
    return views


def _history_views(rows: list[Any], now: int) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for row in rows:
        currency = row["currency"] or ""
        payable = row["total_price"] if row["total_price"] is not None else row["price"]
        when = row["sent_at"] or row["created_at"]
        views.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "status": row["status"],
                "attempts": row["attempts"],
                "error": row["last_error"],
                "when": when,
                "age": _age(max(0, now - when)) if when else "",
                "due_in": (
                    _age(row["next_attempt_at"] - now).removesuffix(" ago")
                    if row["status"] == "pending" and row["next_attempt_at"] > now
                    else None
                ),
                "title": row["title"] or (f"Listing {row['item_id']}" if row["item_id"] else "—"),
                "url": row["url"],
                "photo": row["photo_url"],
                "price": f"{payable:.2f} {currency}".strip() if payable is not None else None,
                "previous_price": (
                    f"{row['previous_price']:.2f} {currency}".strip()
                    if row["kind"] == "price_drop" and row["previous_price"]
                    else None
                ),
                "score": row["enrich_score"],
                "query_name": row["query_name"] or "—",
                "destination": row["destination_name"] or "—",
                "destination_kind": row["destination_kind"] or "",
            }
        )
    return views


def _age(seconds: int) -> str:
    """A found-time a human scans, not arithmetic they have to do."""
    seconds = max(seconds, 0)
    if seconds < 60:  # noqa: PLR2004
        return f"{seconds}s ago"
    if seconds < 3600:  # noqa: PLR2004
        return f"{seconds // 60}m ago"
    if seconds < 86400:  # noqa: PLR2004
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _id_list(raw: str) -> str:
    """Reduce user input to a comma-separated list of numeric ids, dropping the rest."""
    return ",".join(part.strip() for part in raw.split(",") if part.strip().isdigit())


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _percent_or_none(raw: str) -> int | None:
    raw = raw.strip().rstrip("%")
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if 1 <= value <= 100 else None  # noqa: PLR2004


def _redirect_with_notice(message: str) -> Response:
    return RedirectResponse(f"/searches?ok={quote(message)}", status_code=303)


def _int_or_none(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw) or None
    except ValueError:
        return None


def _rating_or_none(raw: str) -> float | None:
    """Accepts 90, 90% or 0.9; stores a fraction, which is what the API reports."""
    raw = raw.strip().rstrip("%")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value > 1.0:
        value /= 100.0
    return value if 0.0 <= value <= 1.0 else None


def _decimal_or_none(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _name_from(params: dict[str, str], tld: str) -> str:
    if text := params.get("search_text"):
        return f"{text} ({tld})"
    return f"vinted.{tld} search"


def _rss_feed(title: str, rows: list[Any]) -> str:
    entries = []
    for row in rows:
        price = row["total_price"] or row["price"]
        description = f"{price} {row['currency'] or ''}".strip()
        entries.append(
            "<item>"
            f"<title>{xml_escape(row['title'] or 'Listing')}</title>"
            f"<link>{xml_escape(row['url'])}</link>"
            f"<guid isPermaLink='false'>{row['item_id']}</guid>"
            f"<description>{xml_escape(description)}</description>"
            "</item>"
        )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'><channel>"
        f"<title>{xml_escape(title)}</title>"
        "<description>Vinted listings matching this search</description>"
        "<link>https://www.vinted.com/</link>"
        f"{''.join(entries)}"
        "</channel></rss>"
    )


class _QuietServer(uvicorn.Server):
    """A server that leaves signal handling to the application.

    uvicorn would otherwise take over SIGTERM and SIGINT, and two sets of handlers means a
    shutdown that only half happens.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(
    settings: Settings,
    repo: Repo,
    stop: asyncio.Event,
    taxonomy: Taxonomy | None = None,
) -> None:
    """Run the web UI until the app shuts down."""

    config = uvicorn.Config(
        create_app(settings, repo, taxonomy),
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,
        access_log=False,
    )
    server = _QuietServer(config)
    serving = asyncio.create_task(server.serve())
    log.info("web.listening", host=settings.web_host, port=settings.web_port)
    if settings.web_is_exposed_without_a_password:
        log.warning(
            "web.no_password",
            host=settings.web_host,
            hint="the dashboard shows your webhook URLs; keep the published port on "
            "127.0.0.1 or set VINTED_SNIPER_WEB_AUTH_TOKEN",
        )
    await stop.wait()
    server.should_exit = True
    await serving
