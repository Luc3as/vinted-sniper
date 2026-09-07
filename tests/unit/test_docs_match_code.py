"""Documentation that drifts is worse than none, so a few claims are checked here."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from vinted_sniper.cli import build_parser
from vinted_sniper.config import Settings
from vinted_sniper.magic.models import MappedQuery, WatchHints

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DOC = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
MAGIC_DOC = (ROOT / "docs" / "magic-search.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("field", sorted(Settings.model_fields))
def test_every_setting_is_documented(field: str) -> None:
    assert field.upper() in CONFIG_DOC, (
        f"{field} exists in Settings but is missing from docs/configuration.md"
    )


@pytest.mark.parametrize("field", sorted(Settings.model_fields))
def test_every_setting_appears_in_the_example_env(field: str) -> None:
    assert f"VINTED_SNIPER_{field.upper()}" in ENV_EXAMPLE, (
        f"{field} exists in Settings but is missing from .env.example"
    )


def test_documented_settings_all_exist() -> None:
    """The opposite direction: nothing documented that was removed from the code."""
    known = {f"VINTED_SNIPER_{name.upper()}" for name in Settings.model_fields}
    mentioned = {
        line.split("=")[0].removeprefix("# ").strip()
        for line in ENV_EXAMPLE.splitlines()
        if "VINTED_SNIPER_" in line and "=" in line
    }

    assert mentioned <= known, f"documented but gone from the code: {sorted(mentioned - known)}"


@pytest.mark.parametrize(
    "command",
    [
        "run",
        "check",
        "watch",
        "searches",
        "unwatch",
        "destination",
        "status",
        "heartbeat",
        "export",
        "import",
    ],
)
def test_commands_named_in_the_docs_exist(command: str) -> None:
    parser = build_parser()
    known = {
        choice
        for action in parser._actions
        for choice in (action.choices or {})
        if isinstance(action.choices, dict)
    }

    assert command in known


def test_the_readme_does_not_promise_buying() -> None:
    """A deliberate non-feature. If this ever fails, the claim and the code disagree."""
    lowered = README.lower()

    assert (
        "cannot buy" in lowered
        or "does not log into your" in lowered
        or "never logs in, buys" in lowered
    )


def _english_half(doc: str) -> str:
    """Everything before the Slovak anchor. The examples only need checking once."""
    return doc.split('<a name="slovensky">', maxsplit=1)[0]


def _json_blocks(doc: str) -> list[Any]:
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)```", doc, re.S)]


@pytest.mark.parametrize(
    "field", sorted(set(MappedQuery.model_fields) | set(WatchHints.model_fields))
)
def test_the_mapper_contract_documents_every_field(field: str) -> None:
    """docs/magic-search.md is the interface the n8n flow is built against.

    A field that exists in the model but is missing from the page is a field the flow
    author never learns about — which, since every field is optional, fails silently.
    """
    assert field in MAGIC_DOC, (
        f"{field} exists in MappedQuery but is missing from docs/magic-search.md"
    )


def test_the_documented_worked_example_is_a_valid_answer() -> None:
    """The example answer must parse, and must produce the params printed beside it."""
    blocks = _json_blocks(_english_half(MAGIC_DOC))
    answers = [b for b in blocks if isinstance(b, dict) and "catalog" in b]
    responses = [b for b in blocks if isinstance(b, dict) and "params" in b]

    assert answers and responses, "the worked example lost its request or its answer"

    mapped = MappedQuery.model_validate(answers[0])

    assert mapped.to_params() == responses[0]["params"]
    assert responses[0]["labels"]["catalog"] == answers[0]["catalog"]["name"]
