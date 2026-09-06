"""Messages in the reader's language.

Alerts, bot replies and health notices can be read in more than one language — a Telegram
chat in Slovak, a Discord server in English — so the language belongs to the destination,
not to the process. Each catalog is one JSON file in `locales/`: msgid → text, or a list of
plural forms. English is the msgid itself, so an untranslated string is never a blank.

This is deliberately gettext-shaped (``gettext`` / ``ngettext`` with ``{name}`` placeholders)
and deliberately not gettext: no .po/.mo compile step, nothing to install in the image, and
Jinja's i18n extension can take these two callables directly when the UI follows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).with_name("locales")
DEFAULT_LANGUAGE = "en"
LANGUAGES: dict[str, str] = {"en": "English", "sk": "Slovenčina"}

# How many plural forms a language has and which one a count picks.
_PLURAL_RULES: dict[str, Callable[[int], int]] = {
    "en": lambda n: 0 if n == 1 else 1,
    # Slovak: 1 / 2-4 / 0 and 5+ (and anything fractional).
    "sk": lambda n: 0 if n == 1 else (1 if 2 <= n <= 4 else 2),  # noqa: PLR2004
}


class Translator:
    """Renders msgids for one language. Missing strings fall back to English."""

    def __init__(self, language: str, catalog: dict[str, Any]) -> None:
        self.language = language
        self._catalog = catalog
        self._plural = _PLURAL_RULES.get(language, _PLURAL_RULES["en"])

    def gettext(self, msgid: str, /, **kwargs: Any) -> str:
        text = self._catalog.get(msgid)
        if not isinstance(text, str):
            text = msgid
        return text.format(**kwargs) if kwargs else text

    def ngettext(self, singular: str, plural: str, n: int, /, **kwargs: Any) -> str:
        forms = self._catalog.get(singular)
        if isinstance(forms, list) and forms:
            text = str(forms[min(self._plural(n), len(forms) - 1)])
        else:
            text = singular if n == 1 else plural
        return text.format(n=n, **kwargs)

    # Short aliases, the way templates and senders like to read.
    __call__ = gettext


@cache
def get(language: str | None) -> Translator:
    """The translator for a language code; English for anything unknown."""
    code = (language or DEFAULT_LANGUAGE).lower()
    if code == DEFAULT_LANGUAGE or code not in LANGUAGES:
        return Translator(DEFAULT_LANGUAGE, {})
    path = LOCALES_DIR / f"{code}.json"
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Translator(DEFAULT_LANGUAGE, {})
    return Translator(code, catalog)


def normalise(language: str | None) -> str:
    """A language code we support, or the default."""
    code = (language or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


EN = get(DEFAULT_LANGUAGE)
