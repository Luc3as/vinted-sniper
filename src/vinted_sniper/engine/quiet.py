"""Quiet hours: a daily window during which a destination is left alone.

A phone that buzzes at 03:14 about a jacket is a phone that gets the bot muted. The window
is written the way people say it — "23:00-07:00" — and is allowed to cross midnight,
because that is when most of them do. Alerts found during the window are held rather than
dropped, so the first message after it ends is a digest of what turned up overnight.

Times are read in the app's configured timezone; see `TIMEZONE` in the settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

_SPEC = re.compile(r"^\s*(\d{1,2}):(\d{1,2})\s*-\s*(\d{1,2}):(\d{1,2})\s*$")


@dataclass(frozen=True, slots=True)
class QuietHours:
    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        """True while the window is open. Handles windows that cross midnight."""
        now = moment.time().replace(second=0, microsecond=0)
        if self.start <= self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse(spec: str | None) -> QuietHours | None:
    """Read "HH:MM-HH:MM". Returns None for blank input; raises ValueError for nonsense."""
    if spec is None or not spec.strip():
        return None
    match = _SPEC.match(spec)
    if match is None:
        raise ValueError("quiet hours must look like 23:00-07:00")
    h1, m1, h2, m2 = (int(part) for part in match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError as exc:
        raise ValueError("quiet hours must use real clock times") from exc
    if start == end:
        raise ValueError("quiet hours cannot start and end at the same time")
    return QuietHours(start, end)


def validate(spec: str) -> str | None:
    """Why a spec will not parse, or None if it is fine."""
    try:
        parse(spec)
    except ValueError as exc:
        return str(exc)
    return None


def is_quiet(spec: str | None, moment: datetime) -> bool:
    """Convenience for callers holding the raw string; an unparseable one is never quiet."""
    try:
        window = parse(spec)
    except ValueError:
        return False
    return window is not None and window.contains(moment)
