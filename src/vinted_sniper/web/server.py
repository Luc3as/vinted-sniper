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
from collections.abc import Callable, Coroutine, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, SecretStr

from vinted_sniper import backup, i18n
from vinted_sniper.config import MIN_POLL_INTERVAL_S, Settings
from vinted_sniper.db.repo import Repo, SweepCandidate, SweepRun
from vinted_sniper.engine import filters, health, quiet, sweep
from vinted_sniper.enrichment import Enrichment, EnrichmentIn
from vinted_sniper.log import get_logger
from vinted_sniper.magic.client import MapperClient
from vinted_sniper.magic.errors import MappingError, TaxonomyUnavailableError
from vinted_sniper.magic.models import WatchHints
from vinted_sniper.magic.triage import TriageClient
from vinted_sniper.magic.validate import validate
from vinted_sniper.magic.verdict import VerdictClient
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.errors import VintedError
from vinted_sniper.vinted.session import SessionManager
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
# How much of the sweep history one page load is allowed to cost. The page fetches each
# run's candidates separately, so these two numbers are what stop a growing sweep_runs
# table turning /history into a fan-out of queries.
SWEEP_HISTORY_RUNS = 5
SWEEP_HISTORY_MATCHES = 3

# Where a full opinion's score changes what a card claims, cut the same way the templates
# cut the 🤖 tag: 75 and up wears green, under 40 red, the band between is a maybe.
_SCORE_GOOD = 75
_SCORE_MID = 40


def _authorised(supplied: str | None, expected: SecretStr | None) -> bool:
    if expected is None:
        return True
    if not supplied:
        return False
    return secrets.compare_digest(supplied, expected.get_secret_value())


class MagicSearchIn(BaseModel):
    """One sentence to map, and which country site to map it against.

    The length cap is a cost guard as much as a validation rule: every mapping is a
    language-model call somebody pays for, and nothing a person types into a search box is
    five hundred characters long.
    """

    text: str = Field(min_length=1, max_length=500)
    tld: str = Field(min_length=2, max_length=8)


class SweepLaunchIn(BaseModel):
    """A confirmed mapping, posted back to start the sweep it describes.

    The map step stores nothing, so the browser hands back what `/api/magic-search/map`
    gave it rather than a mapping id. `labels` is flat here on purpose — it is the plain
    wording the photo check is told to look for, so the confirmation screen sends the names
    it showed the person, one string each.

    The two ceilings are advisory and downward-only. What a sweep is allowed to read and
    pay for is a deployment's decision, so `settings.sweep_max_pages` and
    `settings.sweep_max_items` are the real numbers and anything posted here is clamped to
    them; posting nothing means the settings apply unchanged.
    """

    params: dict[str, str]
    tld: str = Field(min_length=2, max_length=8)
    keywords: list[str] = Field(default_factory=list)
    visual_signature: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    max_pages: int | None = Field(default=None, ge=1)
    max_items: int | None = Field(default=None, ge=1)


class WatchCreateIn(BaseModel):
    """A swept search, posted back to become a standing watch.

    `watch_hints` is a field of its own rather than more keys in `params` for the reason
    `/api/magic-search/map` returned it that way: those are title rules a saved watch
    applies after Vinted answers, and folding them into the search request would narrow the
    very stock the sweep exists to see (R003).

    `sweep_id` is optional because a mapping can be promoted straight from the confirmation
    screen without anyone paying for a sweep first; when it is given, the run row is marked
    with the watch it became.
    """

    params: dict[str, str]
    tld: str = Field(min_length=2, max_length=8)
    name: str = Field(default="", max_length=200)
    watch_hints: WatchHints = Field(default_factory=WatchHints)
    sweep_id: int | None = Field(default=None, ge=1)


async def _run_magic_sweep(
    *,
    sweep_id: int,
    body: SweepLaunchIn,
    tld: str,
    max_pages: int,
    max_items: int,
    settings: Settings,
    repo: Repo,
    client: VintedClient,
    sessions: SessionManager | None,
    triage: TriageClient,
    verdict: VerdictClient | None,
) -> None:
    """The whole of what a web-launched sweep does, at module level so it can be guarded.

    `tests/integration/test_sweep_isolation.py` scans the sweep's write path for the
    poller's own writers, and it scans *this function* rather than `web/server.py` as a
    whole: the enrichment ingest endpoint in this module legitimately calls
    `store_enrichment()`, which is exactly one of the names that must never appear on a
    sweep's side. Scoping the scan to the launcher keeps the guard meaningful at the new
    entry point instead of permanently green.

    It never raises. `judge_sweep()` already degrades rather than throwing, but this runs
    detached from any request, so anything it did not anticipate would otherwise close the
    HTTP story with a run stuck at `status='running'` and a browser polling it forever.
    """
    started = time.monotonic()
    status = "error"
    try:
        result = await sweep.judge_sweep(
            sweep_id=sweep_id,
            tld=tld,
            params=dict(body.params),
            keywords=list(body.keywords),
            visual_signature=body.visual_signature,
            labels=dict(body.labels),
            client=client,
            repo=repo,
            triage=triage,
            verdict=verdict,
            max_pages=max_pages,
            max_items=max_items,
            batch_size=settings.sweep_triage_batch,
            max_verdicts=settings.sweep_max_verdicts,
            sessions=sessions,
            cost_per_mtok_in=settings.magic_cost_per_mtok_in,
            cost_per_mtok_out=settings.magic_cost_per_mtok_out,
        )
        status = result.status
    except Exception as exc:  # a detached task has nowhere to raise to
        # The counts already on the row are kept: whatever pages and batches did land are
        # real, and overwriting them with zeroes would turn a partial result into a lie.
        log.exception("magic.sweep_crashed", sweep_id=sweep_id, error=str(exc))
        status = "partial"
        row = await repo.get_sweep_run(sweep_id)
        if row is not None and row.finished_at is None:
            await repo.finish_sweep_run(
                sweep_id,
                status=status,
                pages_fetched=row.pages_fetched,
                items_seen=row.items_seen,
                candidates=row.candidates,
                funnel=row.funnel,
                error=str(exc),
            )
    finally:
        log.info(
            "magic.sweep_finished",
            sweep_id=sweep_id,
            status=status,
            elapsed_s=round(time.monotonic() - started, 2),
        )


def create_app(
    settings: Settings,
    repo: Repo,
    taxonomy: Taxonomy | None = None,
    mapper: MapperClient | None = None,
    *,
    client: VintedClient | None = None,
    sessions: SessionManager | None = None,
    triage: TriageClient | None = None,
    verdict: VerdictClient | None = None,
    launch: Callable[[Coroutine[Any, Any, None]], None] | None = None,
) -> FastAPI:
    token = settings.web_auth_token  # None means no password: the dashboard just opens

    app = FastAPI(title="vinted-sniper", docs_url=None, redoc_url=None)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(SameOriginMiddleware)
    throttle = LoginThrottle()

    # What a sweep needs, parked where a request can reach it. `sweep.judge_sweep()` wants
    # a live Vinted client, a session manager, and the two n8n clients; the poller process
    # already builds all four, so the web process borrows them rather than opening a second
    # set of connections. Any of them may be None — a bare `create_app(settings, repo)` in a
    # test, or an install with no Magic webhooks configured — and the endpoints that need
    # them say so rather than pretending a sweep is possible.
    app.state.vinted = client
    app.state.sessions = sessions
    app.state.triage = triage
    app.state.verdict = verdict

    # A sweep takes minutes, so the POST that starts one answers 202 and the work runs on
    # after the response. Two things that need holding for that to be true:
    #
    # * The task. `asyncio.create_task()` keeps only a weak reference, so a task nobody
    #   holds can be collected mid-run; `sweep_tasks` is the strong reference, and
    #   `serve()` cancels whatever is still in it on shutdown.
    # * The fact that one is running. A sweep spends real money, so a second click while
    #   the first is still going is refused rather than billed. `sweeping` is claimed
    #   before the first await in the handler, which is what makes the check-then-claim
    #   atomic under asyncio.
    sweep_tasks: set[asyncio.Task[None]] = set()
    app.state.sweep_tasks = sweep_tasks
    sweeping = False

    def _launch(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        sweep_tasks.add(task)
        task.add_done_callback(sweep_tasks.discard)

    # Injectable so a test can run the sweep to completion and then read it back, instead
    # of sleeping and hoping: `TestClient` and a detached task race, and a test that races
    # is a test that will one day be deleted for flaking.
    start_sweep_task = launch if launch is not None else _launch

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
        routes = {
            query_id: set(await repo.destination_ids_for_query(query_id)) for query_id in queries
        }
        watched_tlds = [search.tld for search in snapshot.searches]
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "nav": "searches",
                "snapshot": snapshot,
                "queries": queries,
                "routes": routes,
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

    @app.get("/magic", response_class=HTMLResponse)
    async def magic_page(
        request: Request,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        sweep_id: Annotated[int | None, Query(alias="sweep")] = None,
    ) -> Response:
        """Magic Search: a sentence in, a ranked look at what is already for sale out.

        A sweep's results are rendered here rather than assembled in the browser, so this
        screen and /history say the same thing about the same run — `_sweep_match_view()`
        owns that wording — and so something you paid for survives a reload, as a link you
        can keep. The page's script only polls while a run is still going.
        """
        if not _authorised(session, token):
            return RedirectResponse("/login", status_code=303)

        run = None if sweep_id is None else await repo.get_sweep_run(sweep_id)
        watched_tlds = [query.tld for query in await repo.list_queries()]
        now = int(time.time())
        # The same bounded read /history does: without a list of earlier runs, a sweep's
        # results only exist for whoever kept its link — knowing "?sweep=3" by heart is
        # not a UI. The run being read right now is left out of its own footer.
        recent = await repo.recent_sweep_runs(limit=SWEEP_HISTORY_RUNS)
        recent_candidates = {row.id: await repo.sweep_candidates(row.id) for row in recent}
        past_sweeps = [
            view
            for view in _sweep_views(recent, recent_candidates, now=now)
            if run is None or view["id"] != run.id
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "magic.html",
            {
                "nav": "magic",
                "auth_enabled": token is not None,
                "known_tlds": sorted(urls.KNOWN_TLDS),
                "default_tld": (
                    Counter(watched_tlds).most_common(1)[0][0] if watched_tlds else "fr"
                ),
                # What this deployment is willing to spend, said before the button that
                # spends it — the confirmation step exists for exactly this sentence.
                "max_items": settings.sweep_max_items,
                "max_verdicts": settings.sweep_max_verdicts,
                # Neither half of Magic Search is a server fault when it is missing, but
                # they fail differently: mapping needs the n8n flow and a taxonomy to check
                # its ids against, sweeping needs a live Vinted client and the photo check.
                "mapping_enabled": mapper is not None and taxonomy is not None,
                "sweeping_enabled": client is not None and triage is not None,
                "sweep": (
                    None
                    if run is None
                    else _sweep_run_view(run, await repo.sweep_candidates(run.id), now, limit=None)
                ),
                "past_sweeps": past_sweeps,
                # A link to a sweep this database never had, or one a prune removed: said
                # plainly rather than dropped into an empty page.
                "missing_sweep": sweep_id if sweep_id is not None and run is None else None,
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
        now = int(time.time())
        # Bounded on both axes: the last few runs, and each one's candidates once. A sweep
        # is something a person runs by hand, so this is a handful of small reads.
        runs = await repo.recent_sweep_runs(limit=SWEEP_HISTORY_RUNS)
        candidates = {run.id: await repo.sweep_candidates(run.id) for run in runs}
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "nav": "history",
                "rows": _history_views(rows, now=now),
                "sweeps": _sweep_views(runs, candidates, now=now),
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
        min_enrich_score: Annotated[str, Form()] = "",
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
            min_enrich_score=_percent_or_none(min_enrich_score),
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
        min_enrich_score: Annotated[str, Form()] = "",
        destination_ids: Annotated[list[int] | None, Form()] = None,
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
            min_enrich_score=_percent_or_none(min_enrich_score),
        )
        # The edit form carries the same destination checkboxes as the add form, so a save
        # is also a routing change: unchecked means "stop sending there".
        wanted = set(destination_ids or [])
        current = set(await repo.destination_ids_for_query(query_id))
        for destination_id in current - wanted:
            await repo.unroute(query_id, destination_id)
        for destination_id in wanted - current:
            await repo.route(query_id, destination_id)
        return _redirect_with_notice("Saved. The new settings apply from the next check.")

    @app.post("/searches/interval")
    async def set_every_interval(
        interval: Annotated[str, Form()] = "", _: None = guard
    ) -> Response:
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

    # --- Magic Search --------------------------------------------------------------
    # A sentence in, a params dict out. Two steps, and both can refuse: the n8n flow can
    # be down, answer with a shape this app cannot use, or answer with nothing to search
    # on at all, and the ids it does answer with can be invented. None of those is a server
    # fault, so they come back as 422 with the reason in words — an id that was never
    # checked, or a mapping with no filters in it, would become a search that quietly finds
    # nothing or everything, which is the one outcome this endpoint exists to prevent.
    # The exception is Vinted itself being unreachable while the ids are checked: that is
    # nobody's to fix by rewording, so it is a 502.

    @app.post("/api/magic-search/map")
    async def magic_search_map(body: MagicSearchIn, _: None = guard) -> JSONResponse:
        if mapper is None:
            raise HTTPException(status_code=503, detail="Magic Search is not set up")
        if taxonomy is None:
            raise HTTPException(
                status_code=503,
                detail="Magic Search is not set up: its ids could not be checked",
            )
        tld = _known_tld(body.tld)

        started = time.monotonic()
        try:
            mapped = await mapper.map_query(body.text, tld)
            await validate(mapped, tld=tld, taxonomy=taxonomy)
        except TaxonomyUnavailableError as exc:
            # Before `MappingError`, which it subclasses — caught after its parent this arm
            # would be dead code, and an unreachable Vinted would be blamed on the caller.
            return JSONResponse({"error": str(exc)}, status_code=502)
        except MappingError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except VintedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

        log.info(
            "magic.mapped",
            tld=tld,
            catalog=mapped.catalog.id if mapped.catalog else None,
            brand=mapped.brand.id if mapped.brand else None,
            sizes=len(mapped.sizes),
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        # `labels` carries the names the model returned so a confirmation screen can say
        # "Jackets & Coats / Patagonia / M" instead of three integers, and `watch_hints`
        # travels beside the params rather than inside them: they are title rules for a
        # later standing watch, never search filters (R003).
        return JSONResponse(
            {
                "params": mapped.to_params(),
                "tld": tld,
                "keywords": mapped.keywords,
                "visual_signature": mapped.visual_signature,
                "watch_hints": mapped.watch_hints.model_dump(),
                "labels": {
                    "catalog": mapped.catalog.name if mapped.catalog else None,
                    "brand": mapped.brand.name if mapped.brand else None,
                    "sizes": [size.name for size in mapped.sizes],
                },
            }
        )

    @app.get("/api/sweeps/{sweep_id}")
    async def sweep_detail(sweep_id: int, _: None = guard) -> JSONResponse:
        """One sweep, whole: what it read, what it kept, what it judged and what it spent.

        Deliberately complete rather than minimal — S04's screen polls this while a sweep
        is still running, so a `running` run with three triaged candidates has to be as
        readable as a finished one. The candidates come back in the post-triage order,
        derived by `sweep.triage_order()` from the columns that were persisted rather than
        re-sorted here, so this endpoint cannot drift from what `judge_sweep()` returned.
        """
        run = await repo.get_sweep_run(sweep_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"no sweep numbered {sweep_id}")
        candidates = sweep.triage_order(await repo.sweep_candidates(sweep_id))
        return JSONResponse(
            {
                "id": run.id,
                "status": run.status,
                "tld": run.tld,
                "query_id": run.query_id,
                "params": run.params,
                "keywords": run.keywords,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "pages_fetched": run.pages_fetched,
                "items_seen": run.items_seen,
                # `kept` rather than `candidates`: the list below owns that name.
                "kept": run.candidates,
                "triaged": sum(1 for row in candidates if row.matches_target is not None),
                "verdicts": sum(1 for row in candidates if row.judged_at is not None),
                "funnel": run.funnel,
                "tokens": run.tokens,
                "cost_eur": run.cost_eur,
                "error": run.error,
                "candidates": [_sweep_candidate_view(row) for row in candidates],
            }
        )

    @app.post("/api/magic-search/sweep")
    async def magic_search_sweep(body: SweepLaunchIn, _: None = guard) -> JSONResponse:
        """Start a sweep and answer with the id that describes it, immediately.

        The sweep itself reads pages and buys AI answers for minutes afterwards, so the
        only useful thing to return is something the page can poll — which is why the run's
        row is opened here, before anything is launched, and handed to `judge_sweep()`
        rather than created inside it. Polling `recent_sweep_runs()` for the id a moment
        later would be a race with every other sweep on the same database.
        """
        nonlocal sweeping

        if client is None or triage is None:
            raise HTTPException(status_code=503, detail="Magic Search is not set up")
        tld = _known_tld(body.tld)

        if sweeping:
            log.warning("magic.sweep_launch_refused", reason="in_flight", tld=tld)
            raise HTTPException(
                status_code=409,
                detail="a sweep is already running — wait for it to finish",
            )

        # What the deployment allows, never what the browser asked for. Clamping down is
        # the only direction available: a posted ceiling can shrink a sweep, never grow it.
        max_pages = min(body.max_pages or settings.sweep_max_pages, settings.sweep_max_pages)
        max_items = min(body.max_items or settings.sweep_max_items, settings.sweep_max_items)

        sweeping = True
        try:
            sweep_id = await repo.create_sweep_run(
                tld=tld, params=dict(body.params), keywords=list(body.keywords)
            )
        except Exception:
            sweeping = False
            raise

        async def run() -> None:
            nonlocal sweeping
            try:
                await _run_magic_sweep(
                    sweep_id=sweep_id,
                    body=body,
                    tld=tld,
                    max_pages=max_pages,
                    max_items=max_items,
                    settings=settings,
                    repo=repo,
                    client=client,
                    sessions=sessions,
                    triage=triage,
                    verdict=verdict,
                )
            finally:
                # Cleared in a `finally` rather than in the happy path: a sweep that
                # crashed, or one cancelled by a shutdown, must not wedge the endpoint at
                # 409 for the rest of the process's life.
                sweeping = False

        log.info(
            "magic.sweep_started",
            sweep_id=sweep_id,
            tld=tld,
            max_pages=max_pages,
            max_items=max_items,
        )
        start_sweep_task(run())
        return JSONResponse({"sweep_id": sweep_id}, status_code=202)

    @app.post("/api/magic-search/watch")
    async def magic_search_watch(body: WatchCreateIn, _: None = guard) -> JSONResponse:
        """Promote a mapped (and usually swept) search into a standing watch.

        The end of the Magic Search story: from here on the ordinary poller owns it, so
        what lands in `queries` has to be indistinguishable from a pasted URL. That is why
        the params are re-read out of the canonical URL rather than stored as posted — the
        URL is the uniqueness key, and a row whose params disagreed with its own URL would
        be a watch nobody could have created by pasting.
        """
        tld = _known_tld(body.tld)
        try:
            url = urls.build_search_url(tld, body.params)
        except urls.InvalidSearchURLError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

        if await repo.find_query_by_url(url) is not None:
            # The same wording `POST /searches` uses, because it is the same situation.
            return JSONResponse({"error": "that search is already being watched"}, status_code=409)

        title_pattern = (body.watch_hints.title_pattern or "").strip()
        if title_pattern and (problem := filters.validate_pattern(title_pattern)):
            return JSONResponse(
                {"error": f"title pattern does not compile: {problem}"}, status_code=422
            )

        params = urls.parse_search_params(url)
        query_id = await repo.add_query(
            name=body.name.strip() or _name_from(params, tld),
            url=url,
            tld=tld,
            params=params,
            poll_interval_s=max(settings.poll_default_interval_s, MIN_POLL_INTERVAL_S),
            # The hints become gates on the watch and nothing else — they are deliberately
            # absent from `params` above.
            required_keywords=[word for word in body.watch_hints.required_keywords if word.strip()],
            title_pattern=title_pattern or None,
        )
        if body.sweep_id is not None:
            await repo.attach_sweep_to_query(body.sweep_id, query_id)
        log.info("magic.watch_created", sweep_id=body.sweep_id, query_id=query_id)
        return JSONResponse({"query_id": query_id}, status_code=201)

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


def _destination_config(kind: str, target: str) -> dict[str, Any] | str:  # noqa: PLR0911
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
        # The whole paid opinion, rendered the same way the Telegram alert renders it:
        # the compact summary line, the one-sentence verdict, and the identified product.
        enrichment = Enrichment.from_row(row)
        payable_amount = total if total is not None else price
        payable = Decimal(str(payable_amount)) if payable_amount is not None else None
        verdict_summary = enrichment.summary(payable, currency) if enrichment else None
        state = None
        if enrichment is not None and enrichment.score is not None:
            if enrichment.score >= _SCORE_GOOD:
                state = "good"
            elif enrichment.score >= _SCORE_MID:
                state = "mid"
            else:
                state = "bad"
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
                "verdict_summary": verdict_summary or None,
                "verdict_model": enrichment.model if enrichment else None,
                "verdict_state": state,
                "query_name": row["query_name"],
                "age": _age(now - row["first_seen_at"]),
            }
        )
    return views


def _payable(row: SweepCandidate) -> Decimal | None:
    """What a buyer actually hands over: the total when there is one, the price otherwise."""
    amount = row.total_price if row.total_price is not None else row.price
    return None if amount is None else Decimal(str(amount))


def _sweep_candidate_view(row: SweepCandidate) -> dict[str, Any]:
    """One candidate as JSON: the listing, what the photo check said, and any full opinion.

    `verdict` is `None` until one has been bought, rather than an object full of nulls, so
    "nobody paid for an opinion on this" and "the opinion said nothing" stay distinguishable
    — the same three-valued care `matches_target` needs one field up.
    """
    enrichment = Enrichment.from_candidate(row)
    return {
        "item_id": row.item_id,
        "title": row.title,
        "url": row.url,
        "price": row.price,
        "total_price": row.total_price,
        "currency": row.currency,
        "brand": row.brand,
        "size": row.size,
        "condition": row.condition,
        "photo_url": row.photo_url,
        "thumb_url": row.thumb_url,
        "seller": row.seller_login,
        "stage": row.stage,
        "position": row.position,
        "rank_score": row.rank_score,
        "matches_target": row.matches_target,
        "confidence": row.confidence,
        "triage_reason": row.triage_reason,
        "verdict": None
        if enrichment is None
        else {
            "score": row.verdict_score,
            "model": row.verdict_model,
            "retail_price": row.verdict_retail_price,
            "retail_source": row.verdict_retail_source,
            "matches_query": row.verdict_matches_query,
            "risk": row.verdict_risk,
            "text": row.verdict_text,
            "judged_at": row.judged_at,
            # Rendered by the same code every notification uses, so the API says the same
            # thing the CLI and the alerts do rather than a fourth wording of it.
            "summary": enrichment.summary(_payable(row), row.currency),
        },
    }


def _sweep_run_view(
    run: SweepRun, candidates: list[SweepCandidate], now: int, *, limit: int | None
) -> dict[str, Any]:
    """One sweep as a page prints it: counts, the bill, and its matches in triage order.

    Both readers come through here — /history's summary of the last few runs and /magic's
    screen for the run you just started — so the two pages cannot end up wording the same
    sweep differently. `limit` is the only thing that separates them: history shows the top
    few, the page you paid on shows everything.
    """
    rows = sweep.triage_order(candidates)
    return {
        "id": run.id,
        "status": run.status,
        "tld": run.tld,
        "keywords": ", ".join(run.keywords),
        "params": run.params,
        "query_id": run.query_id,
        "age": _age(max(0, now - run.started_at)),
        "pages_fetched": run.pages_fetched,
        "items_seen": run.items_seen,
        "kept": run.candidates,
        "triaged": sum(1 for row in rows if row.matches_target is not None),
        "verdicts": sum(1 for row in rows if row.judged_at is not None),
        "tokens": run.tokens,
        "cost_eur": f"{run.cost_eur:.4f}",
        "error": run.error,
        "matches": [
            _sweep_match_view(row, run.keywords)
            for row in (rows if limit is None else rows[:limit])
        ],
    }


def _sweep_views(
    runs: list[SweepRun], candidates: dict[int, list[SweepCandidate]], now: int
) -> list[dict[str, Any]]:
    """Recent judged sweeps for the history page: counts, top matches, and the bill.

    Only runs that were actually judged appear. An unjudged sweep is a `sweep` command
    somebody ran without `--judge`; it has no scores and no cost, so a row for it here
    would be an empty row on a page about what the AI concluded.
    """
    views: list[dict[str, Any]] = []
    for run in runs:
        view = _sweep_run_view(run, candidates.get(run.id, []), now, limit=SWEEP_HISTORY_MATCHES)
        if not view["triaged"] and not view["verdicts"]:
            continue
        views.append(view)
    return views


def _sweep_match_view(row: SweepCandidate, keywords: list[str]) -> dict[str, Any]:
    """One of a sweep's top matches, in the words the page prints.

    The photo check compares a photo against the *description* of the thing, not against
    the exact model — a plain rain jacket passes it whether or not it is the model asked
    for. Its answer is worded to say exactly that, so its confidence number cannot be read
    as "this is the one": only a paid full opinion says that, and it says it separately.
    """
    enrichment = Enrichment.from_candidate(row)
    payable = _payable(row)
    currency = row.currency or ""
    if row.matches_target is None:
        photos = "photo not checked"
    else:
        sure = round((row.confidence or 0.0) * 100)
        photos = (
            f"photo fits your description, {sure}% sure"
            if row.matches_target
            else f"photo does not fit your description, {sure}% sure"
        )
    # One colour per card, and the paid opinion owns it. The photo check only guesses at
    # the kind of thing, so its "yes" can never paint a card green by itself: green is a
    # full opinion that scored the listing well, red is one that scored it badly — however
    # sure the photo check sounded. The rank sorts the picks the same way.
    if row.verdict_score is not None:
        if row.verdict_score >= _SCORE_GOOD:
            state, state_rank = "good", 0
        elif row.verdict_score >= _SCORE_MID:
            state, state_rank = "mid", 1
        else:
            state, state_rank = "bad", 3
    elif row.matches_target:
        state, state_rank = "maybe", 2
    elif row.matches_target is None:
        state, state_rank = "unchecked", 4
    else:
        state, state_rank = "no", 4
    return {
        "state": state,
        "state_rank": state_rank,
        "title": row.title,
        "url": row.url,
        "photo": row.thumb_url or row.photo_url,
        "price": f"{payable:.2f} {currency}".strip() if payable is not None else None,
        "match": round(row.rank_score * 100),
        "title_words": sweep.title_words(row.title, keywords),
        "photos": photos,
        "matches_target": row.matches_target,
        "reason": row.triage_reason,
        "score": row.verdict_score,
        "verdict": None if enrichment is None else enrichment.summary(payable, row.currency),
        "verdict_text": row.verdict_text,
    }


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
    mapper: MapperClient | None = None,
    *,
    client: VintedClient | None = None,
    sessions: SessionManager | None = None,
    triage: TriageClient | None = None,
    verdict: VerdictClient | None = None,
) -> None:
    """Run the web UI until the app shuts down."""

    app = create_app(
        settings,
        repo,
        taxonomy,
        mapper,
        client=client,
        sessions=sessions,
        triage=triage,
        verdict=verdict,
    )
    config = uvicorn.Config(
        app,
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

    # A sweep launched by the browser outlives the request that started it, so shutdown has
    # to say so out loud. Uvicorn only knows about requests; these tasks are ours to end,
    # and a run cut off here stays `running` in the database on purpose — it did not
    # finish, and pretending otherwise would put a fabricated result on the history page.
    survivors = [task for task in app.state.sweep_tasks if not task.done()]
    if survivors:
        log.info("web.sweeps_cancelled", count=len(survivors))
        for task in survivors:
            task.cancel()
        await asyncio.gather(*survivors, return_exceptions=True)
