"""The `sweep` command's argument surface, and the promise that it cannot alert.

A sweep is a read: it looks at stock already on Vinted and prints what it found. The
dangerous version of this command is the one that quietly grows an alert write path, so
alongside the ordinary parser assertions there is a source-level check that `_cmd_sweep`
never touches `Dispatcher`, `record_new_items`, `record_price_drops`, `observe_market` or
`work_available`.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import inspect
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vinted_sniper import cli
from vinted_sniper.cli import build_parser
from vinted_sniper.config import (
    SWEEP_MAX_ITEMS_CEILING,
    SWEEP_MAX_PAGES_CEILING,
    Settings,
)
from vinted_sniper.db import Database, apply_pending
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine import sweep
from vinted_sniper.enrichment import EnrichmentIn
from vinted_sniper.vinted.client import PER_PAGE
from vinted_sniper.vinted.models import Item, parse_item

URL = "https://www.vinted.sk/catalog?search_text=torrentshell"

# The alert write path, named exactly as it appears in db/repo.py, engine/poller.py and
# engine/dispatch.py. A sweep reaching any of these would turn a look around into
# notifications nobody asked for.
FORBIDDEN = (
    "Dispatcher",
    "record_new_items",
    "record_price_drops",
    "observe_market",
    "work_available",
)


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_sweep_is_a_registered_subcommand() -> None:
    """Mirrors test_docs_match_code::test_commands_named_in_the_docs_exist."""
    parser = build_parser()
    known = {
        choice
        for action in parser._actions
        for choice in (action.choices or {})
        if isinstance(action.choices, dict)
    }

    assert "sweep" in known


def test_the_url_is_positional_and_the_rest_defaults() -> None:
    args = parse("sweep", URL)

    assert args.command == "sweep"
    assert args.url == URL
    # Zero means "no override" — _cmd_sweep falls back to sweep_max_pages/sweep_max_items.
    assert args.pages == 0
    assert args.max_items == 0
    assert args.keywords == []


def test_the_bounds_can_be_overridden() -> None:
    args = parse("sweep", URL, "--pages", "2", "--max-items", "50")

    assert (args.pages, args.max_items) == (2, 50)


def test_keyword_repeats_into_a_list() -> None:
    args = parse("sweep", URL, "--keyword", "patagonia", "--keyword", "torrentshell")

    assert args.keywords == ["patagonia", "torrentshell"]


def test_the_keyword_default_is_not_shared_between_parses() -> None:
    """argparse's append action mutates the default in place if it is reused."""
    parse("sweep", URL, "--keyword", "patagonia")

    assert parse("sweep", URL).keywords == []


@pytest.mark.parametrize(
    "argv",
    [
        ("sweep",),  # the URL is required
        ("sweep", URL, "--nope"),  # unknown flag
        ("sweep", URL, "--pages", "many"),  # --pages is a number
        ("sweep", URL, "--max-items"),  # flag without its value
    ],
)
def test_bad_invocations_exit_non_zero(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse(*argv)

    assert exit_info.value.code != 0


def test_every_sweep_flag_has_help_text() -> None:
    """The command is meant to be run by hand, so --help has to actually say something."""
    parser = build_parser()
    sweep_parser = next(
        action.choices["sweep"]
        for action in parser._actions
        if isinstance(action.choices, dict) and "sweep" in action.choices
    )

    for action in sweep_parser._actions:
        if action.dest != "help":
            assert action.help, f"{action.dest} has no help text"


def test_the_sweep_command_cannot_reach_the_alert_write_path() -> None:
    # Every function the command is made of, not just its entry point: the guard is only
    # worth anything if extracting a helper cannot quietly move code out from under it.
    source = "\n".join(
        inspect.getsource(part)
        for part in (cli._cmd_sweep, cli._print_sweep, cli._triage_line, cli._verdict_lines)
    )
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
        if isinstance(node, (ast.Name, ast.Attribute))
    }

    assert not names & set(FORBIDDEN), f"sweep reaches the alert path: {names & set(FORBIDDEN)}"


def test_the_sweep_command_is_documented_in_both_languages() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "configuration.md").read_text(
        encoding="utf-8"
    )
    english, _, slovak = doc.partition("# Konfigurácia (slovensky)")

    assert "vinted-sniper sweep" in english
    assert "vinted-sniper sweep" in slovak


# --- --judge ------------------------------------------------------------------------
#
# The flag turns on the two paid stages. Everything above this line is the free sweep and
# must stay true with the flag absent, which is why the parser assertions are duplicated
# here from the other side: `--judge` off by default is the promise that a `sweep` command
# somebody already had in a script never starts spending money.


def test_judge_is_off_unless_it_is_asked_for() -> None:
    assert parse("sweep", URL).judge is False
    assert parse("sweep", URL, "--judge").judge is True


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "sweep.db",
        **overrides,
    )


def test_judge_with_no_photo_check_configured_refuses_in_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It must not fall through to a free sweep: that would look like it worked."""
    code = asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=0, max_items=0, keywords=[], judge=True)
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "photo check is not set up" in captured.err
    assert "MAGIC_TRIAGE_WEBHOOK_URL" in captured.err
    # Nothing was read and nothing was printed: the refusal happens before any request.
    assert captured.out == ""


def test_without_judge_a_missing_photo_check_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is scoped to the flag. A plain sweep never needed the flow."""
    called: list[str] = []

    async def fake_run_sweep(**kwargs: Any) -> sweep.SweepResult:
        called.append("run_sweep")
        return sweep.SweepResult(pages_fetched=1, items_seen=0)

    monkeypatch.setattr(cli.sweep, "run_sweep", fake_run_sweep)
    code = asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=0, max_items=0, keywords=[], judge=False)
    )

    assert (code, called) == (0, ["run_sweep"])


async def _seed_judged_sweep(db_path: Path, item: Item) -> int:
    """A sweep run with one triaged, judged candidate, written the way the engine writes it."""
    async with Database(db_path) as db:
        await apply_pending(db)
        repo = Repo(db)
        sweep_id = await repo.create_sweep_run(tld="sk", params={}, keywords=["torrentshell"])
        await repo.record_sweep_candidates(
            sweep_id, [sweep._to_candidate(sweep.RankedItem(item=item, rank_score=0.0), 0)]
        )
        await repo.record_verdict(
            sweep_id,
            item.item_id,
            EnrichmentIn(score=82, model="Patagonia Torrentshell 3L", verdict="Worth a look."),
            1_760_000_000,
        )
    return sweep_id


def test_the_judged_output_carries_the_photo_verdicts_and_a_cost_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_item: Callable[..., Any],
) -> None:
    """The demo for this slice, without a browser: what was judged, and what it cost."""
    settings = _settings(
        tmp_path, magic_triage_webhook_url="https://n8n.example/webhook/photo-check"
    )
    item = parse_item(make_item(5551234, photo_ts=1_760_000_000, price="48.0"), "sk")
    sweep_id = asyncio.run(_seed_judged_sweep(settings.db_path, item))
    ranked = sweep.RankedItem(
        item=item,
        rank_score=0.0,  # the title said nothing; the photo is what recognised it
        matches_target=True,
        confidence=0.86,
        triage_reason="Grey three-layer shell with the hood described.",
    )
    judged = sweep.SweepResult(
        pages_fetched=2,
        items_seen=40,
        candidates=[ranked],
        sweep_id=sweep_id,
        triaged=1,
        verdicts=1,
        tokens=41840,
        cost_eur=0.0421,
    )

    async def fake_judge_sweep(**kwargs: Any) -> sweep.SweepResult:
        return judged

    monkeypatch.setattr(cli.sweep, "judge_sweep", fake_judge_sweep)
    code = asyncio.run(cli._cmd_sweep(settings, URL, pages=0, max_items=0, keywords=[], judge=True))
    out = capsys.readouterr().out

    assert code == 0
    assert "photos: fits your description, 86% sure" in out
    assert "Grey three-layer shell with the hood described." in out
    # Rendered through Enrichment.summary()/.lines(), not a second renderer.
    assert "verdict: deal 82/100" in out
    assert "Looks like: Patagonia Torrentshell 3L" in out
    assert "Worth a look." in out
    assert (
        "Cost: 1 listing(s) through the filters, 1 photo(s) checked, 1 full opinion(s), "
        "41840 tokens billed — €0.0421" in out
    )


def test_an_untriaged_candidate_says_so_rather_than_reading_as_a_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_item: Callable[..., Any],
) -> None:
    """A batch that never came back is not the photo check saying no (T02/T05)."""
    settings = _settings(
        tmp_path, magic_triage_webhook_url="https://n8n.example/webhook/photo-check"
    )
    item = parse_item(make_item(777, photo_ts=1_760_000_000), "sk")
    judged = sweep.SweepResult(
        pages_fetched=1,
        items_seen=5,
        candidates=[sweep.RankedItem(item=item, rank_score=0.5)],
        sweep_id=0,
    )

    async def fake_judge_sweep(**kwargs: Any) -> sweep.SweepResult:
        return judged

    monkeypatch.setattr(cli.sweep, "judge_sweep", fake_judge_sweep)
    asyncio.run(cli._cmd_sweep(settings, URL, pages=0, max_items=0, keywords=[], judge=True))
    out = capsys.readouterr().out

    assert "photos: not checked" in out
    assert "not this" not in out
    assert "0 photo(s) checked, 0 full opinion(s)" in out


def test_the_judged_output_is_plain_language() -> None:
    """No statistics vocabulary reaches the person running the command."""
    source = inspect.getsource(cli._cmd_sweep) + inspect.getsource(cli._triage_line)
    lowered = source.lower()

    for jargon in ("percentile", "median", "std dev", "z-score"):
        assert jargon not in lowered


# --- The flags cannot outrun the ceilings -------------------------------------------
#
# The environment path was always bounded (Settings.sweep_max_pages is le=10,
# sweep_max_items le=2000). The flags were not: --pages 11 issued eleven requests.


class _CountingClient:
    """Stands in for VintedClient and hands back a full page every time it is asked.

    A full page is what keeps `run_sweep` paging — a short one is its "that was the end"
    signal — and the same 96 listings every time means the item ceiling never fires
    either, so the only thing that can stop the loop is the page ceiling.
    """

    def __init__(self, items: list[Item]) -> None:
        self._items = items
        self.requests: list[dict[str, str]] = []

    async def search(self, tld: str, params: dict[str, str]) -> list[Item]:
        self.requests.append(dict(params))
        return list(self._items)


def test_more_pages_than_the_ceiling_still_reads_only_ten(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_item: Callable[..., Any],
) -> None:
    """--pages 11 must not become eleven requests to Vinted.

    The assertion is on what the client was actually asked for, not on a local variable:
    the defect was a cost paid upstream, so upstream is where it has to be counted.
    """
    page = [parse_item(make_item(i, photo_ts=1_760_000_000), "sk") for i in range(PER_PAGE)]
    client = _CountingClient(page)
    monkeypatch.setattr(cli, "VintedClient", lambda *_args, **_kwargs: client)

    code = asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=11, max_items=0, keywords=[], judge=False)
    )
    out = capsys.readouterr().out

    assert code == 0
    assert len(client.requests) == SWEEP_MAX_PAGES_CEILING, (
        f"asked Vinted for {len(client.requests)} page(s), ceiling is {SWEEP_MAX_PAGES_CEILING}"
    )
    assert [req["page"] for req in client.requests] == [str(n) for n in range(1, 11)]
    # Clamped, not silently: the number asked for, the number given, and why.
    assert "You asked for 11 page(s); reading 10." in out
    assert "another request to Vinted" in out
    assert "Reading up to 10 page(s)" in out


def test_a_page_count_under_the_ceiling_is_left_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    make_item: Callable[..., Any],
) -> None:
    """The clamp is a ceiling, not a rewrite: --pages 2 is still two requests."""
    page = [parse_item(make_item(i, photo_ts=1_760_000_000), "sk") for i in range(PER_PAGE)]
    client = _CountingClient(page)
    monkeypatch.setattr(cli, "VintedClient", lambda *_args, **_kwargs: client)

    asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=2, max_items=0, keywords=[], judge=False)
    )
    out = capsys.readouterr().out

    assert len(client.requests) == 2
    assert "You asked for" not in out


def test_more_listings_than_the_ceiling_is_bounded_at_two_thousand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--max-items 5000 is what the later AI stages would have been asked to pay for."""
    seen: dict[str, Any] = {}

    async def fake_run_sweep(**kwargs: Any) -> sweep.SweepResult:
        seen.update(kwargs)
        return sweep.SweepResult(pages_fetched=1, items_seen=0)

    monkeypatch.setattr(cli.sweep, "run_sweep", fake_run_sweep)
    code = asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=0, max_items=5000, keywords=[], judge=False)
    )
    out = capsys.readouterr().out

    assert code == 0
    assert seen["max_items"] == SWEEP_MAX_ITEMS_CEILING
    assert "You asked to look at 5000 listing(s); looking at 2000." in out
    assert "at most 2000 listing(s)" in out


def test_a_listing_count_under_the_ceiling_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run_sweep(**kwargs: Any) -> sweep.SweepResult:
        seen.update(kwargs)
        return sweep.SweepResult(pages_fetched=1, items_seen=0)

    monkeypatch.setattr(cli.sweep, "run_sweep", fake_run_sweep)
    asyncio.run(
        cli._cmd_sweep(_settings(tmp_path), URL, pages=0, max_items=50, keywords=[], judge=False)
    )
    out = capsys.readouterr().out

    assert seen["max_items"] == 50
    assert "You asked to look at" not in out
