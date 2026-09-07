"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from vinted_sniper import __version__, app, backup, log
from vinted_sniper.config import (
    MIN_POLL_INTERVAL_S,
    SWEEP_MAX_ITEMS_CEILING,
    SWEEP_MAX_PAGES_CEILING,
    Settings,
)
from vinted_sniper.db import Database, apply_pending
from vinted_sniper.db.repo import Repo, SweepCandidate
from vinted_sniper.engine import filters, health, quiet, sweep
from vinted_sniper.enrichment import Enrichment
from vinted_sniper.magic.triage import TriageClient
from vinted_sniper.magic.verdict import VerdictClient
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.client import VintedClient
from vinted_sniper.vinted.errors import BlockedError, VintedError
from vinted_sniper.vinted.models import Item
from vinted_sniper.vinted.session import SessionManager
from vinted_sniper.vinted.transport import TransportSession

TROUBLESHOOTING = "https://github.com/jasp-nerd/vinted-sniper/blob/main/docs/troubleshooting.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vinted-sniper",
        description="Watch Vinted searches and get notified when something matches.",
    )
    parser.add_argument("--version", action="version", version=f"vinted-sniper {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start watching. This is what the container runs.")
    sub.add_parser("migrate", help="Create or update the database, then exit.")
    sub.add_parser("status", help="Show how each search is doing.")

    sub.add_parser("export", help="Print searches, destinations and routes as JSON.")
    imp = sub.add_parser("import", help="Add searches, destinations and routes from an export.")
    imp.add_argument("file", help="Path to a JSON file produced by 'export', or - for stdin.")
    sub.add_parser("heartbeat", help="Exit 0 if the app is alive. Used by the health check.")

    check = sub.add_parser(
        "check",
        help="Fetch one search once and print what came back. Use this to prove the "
        "connection works before setting anything up.",
    )
    check.add_argument(
        "--url", required=True, help="A Vinted search URL, copied from your browser."
    )

    sweep_cmd = sub.add_parser(
        "sweep",
        help="Look once through what is already for sale and print the best matches. "
        "Nothing is saved as a search and nobody is notified.",
    )
    sweep_cmd.add_argument("url", help="A Vinted search URL, copied from your browser.")
    sweep_cmd.add_argument(
        "--pages",
        type=int,
        default=0,
        help="How many pages to read (default from settings).",
    )
    sweep_cmd.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="The most listings to look at (default from settings).",
    )
    sweep_cmd.add_argument(
        "--keyword",
        action="append",
        default=[],
        dest="keywords",
        help="A word that makes a listing a better match. Repeat it for more words. "
        "Missing words never remove a listing, they only push it down the list.",
    )
    sweep_cmd.add_argument(
        "--judge",
        action="store_true",
        help="Also look at the photos: every listing that survives the filters goes to the "
        "photo check, the best matches are ranked by what they look like rather than by "
        "what the seller called them, and the top few get a full opinion. Costs money and "
        "needs the photo check set up.",
    )

    watch = sub.add_parser("watch", help="Add a search.")
    watch.add_argument("url", help="A Vinted search URL, copied from your browser.")
    watch.add_argument("--name", default="", help="What to call it.")
    watch.add_argument(
        "--every", type=int, default=0, help="Seconds between checks (default from settings)."
    )
    watch.add_argument(
        "--max-price",
        default="",
        help="Skip anything above this, buyer protection included.",
    )
    watch.add_argument("--exclude", default="", help="Comma-separated words to skip in titles.")
    watch.add_argument(
        "--require", default="", help="Comma-separated words that must all appear in the title."
    )
    watch.add_argument(
        "--title-regex",
        default="",
        help="A regular expression the title must match (case-insensitive).",
    )
    watch.add_argument(
        "--min-seller-rating",
        default="",
        help="Skip sellers rated below this, as a percentage (e.g. 90).",
    )
    watch.add_argument(
        "--min-seller-reviews", type=int, default=0, help="Skip sellers with fewer reviews."
    )
    watch.add_argument(
        "--block-seller", default="", help="Comma-separated seller usernames to skip."
    )
    watch.add_argument(
        "--cheapest",
        type=int,
        default=0,
        help="Only listings in the cheapest N%% of what this search has seen in 30 days.",
    )
    watch.add_argument(
        "--to",
        default="",
        help="Comma-separated destination ids to notify. Defaults to all active ones.",
    )

    sub.add_parser("searches", help="List saved searches.")

    unwatch = sub.add_parser("unwatch", help="Remove a search.")
    unwatch.add_argument("query_id", type=int)

    destination = sub.add_parser("destination", help="Add somewhere to send notifications.")
    destination.add_argument("kind", choices=["discord", "telegram", "webhook", "ntfy"])
    destination.add_argument(
        "target", help="Webhook URL, Telegram chat id, ntfy topic, or endpoint URL."
    )
    destination.add_argument("--name", default="", help="What to call it.")
    destination.add_argument(
        "--status", action="store_true", help="Also send health warnings here."
    )
    destination.add_argument(
        "--lang",
        default="en",
        choices=["en", "sk"],
        help="Language of alerts, bot replies and notices sent here.",
    )
    destination.add_argument(
        "--quiet",
        default="",
        help="Daily window to send nothing, e.g. 23:00-07:00 (in TIMEZONE). "
        "Alerts found meanwhile arrive as a digest when it ends.",
    )

    sub.add_parser("destinations", help="List destinations.")

    pair = sub.add_parser(
        "pair-telegram",
        help="Print a link that connects a Telegram chat without hunting for its chat id.",
    )
    pair.add_argument("--name", default="Telegram", help="What to call the destination.")
    pair.add_argument("--bot-username", default="", help="Your bot's @username, without the @.")

    return parser


# --- Commands ----------------------------------------------------------------------


async def _cmd_migrate(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        applied = await apply_pending(db)
    print(f"Database ready at {settings.db_path} ({applied} migration(s) applied).")
    return 0


async def _cmd_check(settings: Settings, url: str) -> int:
    try:
        normalised = urls.normalise_search_url(url)
        tld = urls.extract_tld(normalised)
        params = urls.parse_search_params(normalised)
    except urls.InvalidSearchURLError as exc:
        print(f"That URL will not work: {exc}", file=sys.stderr)
        return 2

    print(f"Site:   vinted.{tld}")
    print(f"Search: {normalised}")
    print(f"Params: {params}\n")

    async with Database(settings.db_path) as db:
        await apply_pending(db)
        async with TransportSession.build(
            impersonate=settings.http_impersonate,
            timeout=settings.request_timeout_s,
            mock_dir=settings.mock_scenario_dir if settings.fetch_mode == "mock" else None,
        ) as transport:
            sessions = SessionManager(
                db,
                transport,
                rotate_after_minutes=settings.session_rotate_minutes,
                impersonate=settings.http_impersonate,
            )
            client = VintedClient(transport, sessions)

            try:
                items = await client.search(tld, params)
            except BlockedError as exc:
                print(f"Blocked: {exc}\n", file=sys.stderr)
                print(
                    "This usually means the address you are connecting from is being "
                    "challenged rather than anything about your search. Confirm it with:\n"
                    f"  curl -v -c - -L https://www.vinted.{tld}/ 2>&1 | grep access_token_web\n"
                    f"If that prints nothing, see {TROUBLESHOOTING}",
                    file=sys.stderr,
                )
                return 1
            except VintedError as exc:
                print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1

    if not items:
        print("Connected fine, but the search returned no listings.")
        print("Try a broader search to confirm everything works.")
        return 0

    print(f"Got {len(items)} listing(s). The newest few:\n")
    for item in items[:5]:
        listed = item.listed_at.strftime("%Y-%m-%d %H:%M UTC") if item.listed_at else "unknown"
        details = " · ".join(filter(None, [item.brand, item.size, item.condition]))
        print(f"  {item.title}")
        print(f"    {item.price_line()}")
        if details:
            print(f"    {details}")
        print(f"    listed {listed}")
        print(f"    {item.url}\n")
    return 0


async def _cmd_sweep(
    settings: Settings,
    url: str,
    *,
    pages: int,
    max_items: int,
    keywords: list[str],
    judge: bool = False,
) -> int:
    """One read of stock already on sale, ranked. Nothing is watched and nobody is told.

    Deliberately built from the same pieces as `_cmd_check`: Database -> apply_pending ->
    TransportSession -> SessionManager -> VintedClient, and nothing else. There is no
    `Dispatcher` here, no `Repo.record_new_items` / `record_price_drops` / `observe_market`
    and no `work_available` event — that trio plus the event is the alert write path
    (`db/repo.py`, woken from `engine/poller.py`), and a sweep touching any of it would
    turn a read-only look around into notifications nobody asked for. A sweep writes only
    to the `sweep_runs` / `sweep_candidates` tables, which the poller never reads.

    `--judge` adds the two paid stages on top of exactly that: the same pieces, plus a
    photo check and a few full opinions, still written only to the sweep tables. Without
    the flag not one byte of this changes — no client is built, no request is made.
    """
    if judge and not settings.magic_triage_webhook_url:
        # Refused rather than quietly downgraded to a free sweep. Somebody who typed
        # --judge asked for the photo check; running without it would look like it worked.
        print(
            "The photo check is not set up, so --judge has nothing to ask. Set "
            "VINTED_SNIPER_MAGIC_TRIAGE_WEBHOOK_URL to the n8n flow that looks at listing "
            "photos, or run the same command without --judge for a free sweep.",
            file=sys.stderr,
        )
        return 2

    try:
        normalised = urls.normalise_search_url(url)
        tld = urls.extract_tld(normalised)
        params = urls.parse_search_params(normalised)
    except urls.InvalidSearchURLError as exc:
        print(f"That URL will not work: {exc}", file=sys.stderr)
        return 2

    # Clamped here rather than in argparse: the flags are only one caller. Anything that
    # reaches _cmd_sweep in-process gets the same ceilings, and the clamp stays inline so
    # the source-level guard in tests/unit/test_cli_sweep.py keeps covering this function.
    max_pages = pages if pages > 0 else settings.sweep_max_pages
    if max_pages > SWEEP_MAX_PAGES_CEILING:
        print(
            f"You asked for {max_pages} page(s); reading {SWEEP_MAX_PAGES_CEILING}. "
            "Every page is another request to Vinted, and asking for too many in one go "
            "is how a sweep gets blocked. Narrow the search and run it again to reach "
            "further back."
        )
        max_pages = SWEEP_MAX_PAGES_CEILING
    ceiling = max_items if max_items > 0 else settings.sweep_max_items
    if ceiling > SWEEP_MAX_ITEMS_CEILING:
        print(
            f"You asked to look at {ceiling} listing(s); looking at "
            f"{SWEEP_MAX_ITEMS_CEILING}. That is as much as one sweep will read, so the "
            "run has a knowable cost. Narrow the search and run it again for the rest."
        )
        ceiling = SWEEP_MAX_ITEMS_CEILING
    # With no --keyword the words you typed into Vinted are the ones that rank. They are a
    # hint either way: a listing missing all of them still gets stored, just last.
    ranking = keywords or params.get("search_text", "").split()
    triage_url = settings.magic_triage_webhook_url

    print(f"Site:   vinted.{tld}")
    print(f"Search: {normalised}")
    print(f"Rank by: {', '.join(ranking) if ranking else '(nothing — everything ties)'}")
    print(f"Reading up to {max_pages} page(s), at most {ceiling} listing(s).\n")

    async with Database(settings.db_path) as db:
        await apply_pending(db)
        async with TransportSession.build(
            impersonate=settings.http_impersonate,
            timeout=settings.request_timeout_s,
            mock_dir=settings.mock_scenario_dir if settings.fetch_mode == "mock" else None,
        ) as transport:
            sessions = SessionManager(
                db,
                transport,
                rotate_after_minutes=settings.session_rotate_minutes,
                impersonate=settings.http_impersonate,
            )
            client = VintedClient(transport, sessions)
            repo = Repo(db)

            # Built here rather than in a helper so the source-level guard in
            # tests/unit/test_cli_sweep.py, which walks this function's own AST, keeps
            # covering the judging path too.
            triage_client = (
                TriageClient(
                    triage_url,
                    token=settings.magic_webhook_token,
                    timeout_s=settings.magic_timeout_s,
                )
                if judge and triage_url
                else None
            )
            verdict_client = (
                VerdictClient(
                    settings.magic_verdict_webhook_url,
                    token=settings.magic_webhook_token,
                    timeout_s=settings.magic_timeout_s,
                )
                if triage_client is not None and settings.magic_verdict_webhook_url
                else None
            )

            try:
                if triage_client is not None:
                    result = await sweep.judge_sweep(
                        tld=tld,
                        params=params,
                        keywords=ranking,
                        # A sweep from a URL has no mapped query behind it, so there is no
                        # description of what the thing looks like and no id names to hand
                        # over. The photo check gets the words and nothing invented.
                        visual_signature=None,
                        labels={},
                        client=client,
                        repo=repo,
                        triage=triage_client,
                        verdict=verdict_client,
                        max_pages=max_pages,
                        max_items=ceiling,
                        batch_size=settings.sweep_triage_batch,
                        max_verdicts=settings.sweep_max_verdicts,
                        sessions=sessions,
                        cost_per_mtok_in=settings.magic_cost_per_mtok_in,
                        cost_per_mtok_out=settings.magic_cost_per_mtok_out,
                    )
                else:
                    result = await sweep.run_sweep(
                        tld=tld,
                        params=params,
                        keywords=ranking,
                        client=client,
                        repo=repo,
                        max_pages=max_pages,
                        max_items=ceiling,
                        sessions=sessions,
                    )
            finally:
                if triage_client is not None:
                    await triage_client.aclose()
                if verdict_client is not None:
                    await verdict_client.aclose()

            # The full opinions were written to the candidate rows, not onto the ranking,
            # so they are read back before the database closes.
            stored = (
                {row.item_id: row for row in await repo.sweep_candidates(result.sweep_id)}
                if judge and result.sweep_id
                else {}
            )

    _print_sweep(result, stored, judge=judge)

    if result.status == "blocked":
        print(f"Stopped early: the site refused the request ({result.error}).", file=sys.stderr)
        print(f"See {TROUBLESHOOTING}", file=sys.stderr)
        return 1
    if result.status != "ok":
        print(f"Stopped early: {result.error}", file=sys.stderr)
        print("What you see above is only what it managed to read.", file=sys.stderr)
        return 1
    return 0


def _print_sweep(
    result: sweep.SweepResult,
    stored: dict[int, SweepCandidate],
    *,
    judge: bool,
) -> None:
    """Everything one sweep has to say, in the order somebody reads it.

    Split out of `_cmd_sweep` for length, and only the printing was split: every call that
    touches the database or the site stays inside the function the source-level guard in
    `tests/unit/test_cli_sweep.py` walks. The judged lines are additive — with `judge`
    false this prints exactly what it always printed, byte for byte.
    """
    print(
        f"Sweep #{result.sweep_id}: read {result.pages_fetched} page(s), "
        f"saw {result.items_seen} listing(s), kept {len(result.candidates)}."
    )
    if result.funnel:
        print("\nSkipped:")
        for reason, count in sorted(result.funnel.items(), key=lambda pair: -pair[1]):
            print(f"  {count} x {reason}")

    if result.candidates:
        print("\nBest matches:\n")
        for ranked in result.candidates[:5]:
            item = ranked.item
            print(f"  {item.title}")
            print(f"    match {round(ranked.rank_score * 100)}% · {item.price_line()}")
            if judge:
                print(f"    {_triage_line(ranked)}")
                if ranked.triage_reason:
                    print(f"    {ranked.triage_reason}")
                for line in _verdict_lines(stored.get(item.item_id), item):
                    print(f"    {line}")
            print(f"    {item.url}\n")
    else:
        print("\nNothing survived the filters. Try a broader search or a higher budget.")

    if judge:
        # One line saying what the whole run spent, matching the `sweep.cost` log line the
        # engine emits. Four counts and a price, because that is what deciding whether to
        # run it again actually needs.
        print(
            f"Cost: {len(result.candidates)} listing(s) through the filters, "
            f"{result.triaged} photo(s) checked, {result.verdicts} full opinion(s), "
            f"{result.tokens} tokens billed — €{result.cost_eur:.4f}"
        )


def _triage_line(ranked: sweep.RankedItem) -> str:
    """What the photo check made of one listing, in words rather than a boolean.

    Three-valued, like the column behind it: "not checked" is not "not this". A batch that
    never came back must not read as a rejection (T02/T05).
    """
    if ranked.matches_target is None:
        return "photos: not checked"
    verdict = "looks like it" if ranked.matches_target else "not this"
    return f"photos: {verdict}, {round((ranked.confidence or 0.0) * 100)}% sure"


def _verdict_lines(candidate: SweepCandidate | None, item: Item) -> list[str]:
    """The full opinion, rendered by the same code every notification uses.

    `Enrichment.summary()` / `.lines()` are what Telegram, Discord and the webhooks already
    print, and they take a `Translator`, so a second renderer here would be a second thing
    to translate and a second thing to keep in step. A sweep candidate is not an `items`
    row, which is the only reason `from_candidate` exists beside `from_row`.
    """
    enrichment = Enrichment.from_candidate(candidate) if candidate is not None else None
    if enrichment is None:
        return []
    payable = item.total_price if item.total_price is not None else item.price
    summary, details = enrichment.lines(payable, item.currency)
    if not summary and not details:
        return []
    return [f"verdict: {summary}" if summary else "verdict:", *(f"  {line}" for line in details)]


async def _cmd_watch(settings: Settings, args: argparse.Namespace) -> int:
    try:
        normalised = urls.normalise_search_url(args.url)
        tld = urls.extract_tld(normalised)
        params = urls.parse_search_params(normalised)
    except urls.InvalidSearchURLError as exc:
        print(f"That URL will not work: {exc}", file=sys.stderr)
        return 2

    max_price: Decimal | None = None
    if args.max_price:
        try:
            max_price = Decimal(args.max_price)
        except InvalidOperation:
            print(f"{args.max_price!r} is not a number.", file=sys.stderr)
            return 2

    async with Database(settings.db_path) as db:
        await apply_pending(db)
        repo = Repo(db)

        if await repo.find_query_by_url(normalised) is not None:
            print("That search is already being watched.", file=sys.stderr)
            return 1

        if args.title_regex and (problem := filters.validate_pattern(args.title_regex)):
            print(f"--title-regex does not compile: {problem}", file=sys.stderr)
            return 1
        min_rating = _rating_or_none(args.min_seller_rating)
        if args.min_seller_rating and min_rating is None:
            print("--min-seller-rating must be a number between 0 and 100", file=sys.stderr)
            return 1

        interval = max(args.every or settings.poll_default_interval_s, MIN_POLL_INTERVAL_S)
        query_id = await repo.add_query(
            name=args.name.strip() or (params.get("search_text") or f"vinted.{tld}"),
            url=normalised,
            tld=tld,
            params=params,
            poll_interval_s=interval,
            banned_keywords=_csv(args.exclude),
            max_total_price=max_price,
            required_keywords=_csv(args.require),
            title_pattern=args.title_regex.strip() or None,
            min_seller_rating=min_rating,
            min_seller_reviews=args.min_seller_reviews or None,
            blocked_sellers=_csv(args.block_seller),
            max_market_percentile=args.cheapest or None,
        )

        if args.to.strip():
            destination_ids = [int(part) for part in args.to.split(",") if part.strip()]
        else:
            destination_ids = [d.id for d in await repo.list_destinations(active_only=True)]
        for destination_id in destination_ids:
            await repo.route(query_id, destination_id)

    print(f"Watching “{args.name or params.get('search_text') or normalised}” (id {query_id}).")
    print(f"Checking vinted.{tld} every {interval}s.")
    if not destination_ids:
        print("\nNo destinations yet — add one with: vinted-sniper destination discord <url>")
    return 0


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _rating_or_none(raw: str) -> float | None:
    """A rating typed as a percentage (90) or a fraction (0.9), stored as a fraction."""
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


async def _cmd_searches(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        queries = await Repo(db).list_queries()
    if not queries:
        print("No searches yet.")
        return 0
    for query in queries:
        flags = " (paused)" if query.paused else ""
        limit = f", max {query.max_total_price}" if query.max_total_price else ""
        print(
            f"{query.id}: {query.name}{flags} — vinted.{query.tld}, "
            f"every {query.poll_interval_s}s{limit}"
        )
        print(f"    {query.url}")
    return 0


async def _cmd_unwatch(settings: Settings, query_id: int) -> int:
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        repo = Repo(db)
        if await repo.get_query(query_id) is None:
            print(f"No search with id {query_id}.", file=sys.stderr)
            return 1
        await repo.delete_query(query_id)
    print(f"Removed search {query_id}.")
    return 0


async def _cmd_destination(settings: Settings, args: argparse.Namespace) -> int:
    config: dict[str, str]
    match args.kind:
        case "discord":
            config = {"webhook_url": args.target}
        case "telegram":
            config = {"chat_id": args.target}
        case "webhook":
            config = {"url": args.target}
        case _:
            config = {"topic": args.target}

    if args.quiet and (problem := quiet.validate(args.quiet)):
        print(f"--quiet: {problem}", file=sys.stderr)
        return 1

    async with Database(settings.db_path) as db:
        await apply_pending(db)
        destination_id = await Repo(db).add_destination(
            kind=args.kind,
            name=args.name.strip() or args.kind,
            config=config,
            notify_status=args.status,
            quiet_hours=args.quiet.strip() or None,
            language=args.lang,
        )
    print(f"Added {args.kind} destination (id {destination_id}).")
    print("Route a search to it with: vinted-sniper watch <url> --to " + str(destination_id))
    return 0


async def _cmd_destinations(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        destinations = await Repo(db).list_destinations()
    if not destinations:
        print("No destinations yet.")
        return 0
    for destination in destinations:
        state = "active" if destination.active else "disabled"
        print(f"{destination.id}: {destination.name} ({destination.kind}, {state})")
    return 0


async def _cmd_pair_telegram(settings: Settings, args: argparse.Namespace) -> int:
    if settings.telegram_bot_token is None:
        print(
            "Set VINTED_SNIPER_TELEGRAM_BOT_TOKEN first. Talk to @BotFather on Telegram to "
            "create a bot and get one.",
            file=sys.stderr,
        )
        return 2

    from vinted_sniper.botctl.telegram_bot import create_pairing  # noqa: PLC0415

    async with Database(settings.db_path) as db:
        await apply_pending(db)
        _, code = await create_pairing(Repo(db), args.name)

    username = args.bot_username.lstrip("@")
    print("Open this link in Telegram, in the chat or group you want alerts in:\n")
    if username:
        print(f"  https://t.me/{username}?start={code}\n")
    else:
        print(f"  https://t.me/<your bot's username>?start={code}\n")
        print("Pass --bot-username to have that filled in for you.\n")
    print("The app must be running for the link to work. It expires in 30 minutes.")
    return 0


async def _cmd_status(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        snapshot = await health.snapshot(Repo(db))

    print("Running." if snapshot.alive else "Not running (or the heartbeat is stale).")
    if not snapshot.searches:
        print("No searches set up yet.")
        return 0

    now = int(time.time())
    for search in snapshot.searches:
        last = f"{now - search.last_success_at}s ago" if search.last_success_at else "never"
        print(f"\n{search.name} [{search.state}] — vinted.{search.tld}")
        print(f"  last successful check: {last}")
        if search.newest_listing_at:
            print(f"  newest listing seen:   {now - search.newest_listing_at}s ago")
        if search.blocks or search.rate_limits:
            print(f"  blocked {search.blocks} times, rate limited {search.rate_limits} times")
        if search.last_error:
            print(f"  last error: {search.last_error}")
    if snapshot.queued_notifications:
        print(f"\n{snapshot.queued_notifications} notification(s) waiting to send.")
    return 0


async def _cmd_heartbeat(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        return 0 if await health.is_alive(Repo(db)) else 1


async def _cmd_export(settings: Settings) -> int:
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        document = await backup.export_config(Repo(db))
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


async def _cmd_import(settings: Settings, path: str) -> int:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()  # noqa: ASYNC230, SIM115
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"not JSON: {exc}", file=sys.stderr)
        return 1
    async with Database(settings.db_path) as db:
        await apply_pending(db)
        try:
            added = await backup.import_config(Repo(db), document)
        except (ValueError, KeyError, urls.InvalidSearchURLError) as exc:
            print(f"import failed: {exc}", file=sys.stderr)
            return 1
    print(
        f"Added {added['destinations']} destination(s), {added['searches']} search(es), "
        f"{added['routes']} route(s). Existing ones were left alone."
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    log.configure(level=settings.log_level, fmt=settings.log_format, colors=settings.log_color)

    match args.command:
        case "run":
            await app.run(settings)
            return 0
        case "migrate":
            return await _cmd_migrate(settings)
        case "check":
            return await _cmd_check(settings, args.url)
        case "sweep":
            return await _cmd_sweep(
                settings,
                args.url,
                pages=args.pages,
                max_items=args.max_items,
                keywords=list(args.keywords),
                judge=args.judge,
            )
        case "watch":
            return await _cmd_watch(settings, args)
        case "searches":
            return await _cmd_searches(settings)
        case "unwatch":
            return await _cmd_unwatch(settings, args.query_id)
        case "destination":
            return await _cmd_destination(settings, args)
        case "destinations":
            return await _cmd_destinations(settings)
        case "pair-telegram":
            return await _cmd_pair_telegram(settings, args)
        case "status":
            return await _cmd_status(settings)
        case "export":
            return await _cmd_export(settings)
        case "import":
            return await _cmd_import(settings, args.file)
        case "heartbeat":
            return await _cmd_heartbeat(settings)
        case unknown:
            raise AssertionError(f"unhandled command {unknown!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
