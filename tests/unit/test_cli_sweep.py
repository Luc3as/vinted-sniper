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
import inspect
import textwrap
from pathlib import Path

import pytest

from vinted_sniper import cli
from vinted_sniper.cli import build_parser

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
    source = inspect.getsource(cli._cmd_sweep)
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
