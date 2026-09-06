"""Messages in the reader's language: catalogs stay in step with the code, plurals bend."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vinted_sniper import i18n

SRC = Path(__file__).resolve().parents[2] / "src" / "vinted_sniper"
CATALOG = json.loads((i18n.LOCALES_DIR / "sk.json").read_text(encoding="utf-8"))

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
MIN_CHUNK = 6


def test_english_is_the_identity() -> None:
    t = i18n.get("en")
    assert t("Open listing") == "Open listing"
    assert t("Seller: {seller}", seller="bob") == "Seller: bob"
    assert t.ngettext("{n} more matches", "{n} more matches", 1) == "1 more matches"


def test_unknown_languages_fall_back_to_english() -> None:
    assert i18n.get("xx").language == "en"
    assert i18n.get(None).language == "en"
    assert i18n.normalise("SK") == "sk"
    assert i18n.normalise("klingon") == "en"


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "1 nový inzerát"),
        (2, "2 nové inzeráty"),
        (4, "4 nové inzeráty"),
        (5, "5 nových inzerátov"),
        (0, "0 nových inzerátov"),
    ],
)
def test_slovak_has_three_plural_forms(n: int, expected: str) -> None:
    assert i18n.get("sk").ngettext("{n} new listing", "{n} new listings", n) == expected


def test_a_missing_slovak_string_falls_back_to_english_rather_than_blank() -> None:
    assert i18n.get("sk")("Something nobody translated") == "Something nobody translated"


@pytest.mark.parametrize("msgid", sorted(CATALOG))
def test_every_slovak_string_keeps_its_placeholders(msgid: str) -> None:
    value = CATALOG[msgid]
    forms = value if isinstance(value, list) else [value]
    wanted = set(_PLACEHOLDER.findall(msgid)) | ({"n"} if isinstance(value, list) else set())
    for form in forms:
        assert set(_PLACEHOLDER.findall(form)) <= wanted, f"{msgid!r} → {form!r}"


@pytest.mark.parametrize("msgid", sorted(CATALOG))
def test_every_slovak_string_is_still_used_by_the_code(msgid: str) -> None:
    """A translation for a string that no longer exists is dead weight and a sign the
    English changed without the catalog following."""
    first_line = msgid.split("\n", maxsplit=1)[0]
    chunks = [c.strip() for c in _PLACEHOLDER.split(first_line) if len(c.strip()) >= MIN_CHUNK]
    if not chunks:
        pytest.skip("too short to locate")
    # The code may wrap a long string across lines, so a short chunk is enough.
    needle = max(chunks, key=len)[:24].rstrip(" (")
    haystack = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    assert needle in haystack, f"{msgid!r} is translated but not found in the code"
