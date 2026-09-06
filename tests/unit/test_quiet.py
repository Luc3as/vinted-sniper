"""Quiet hours: the window is read the way people write it, midnight included."""

from __future__ import annotations

from datetime import datetime

import pytest

from vinted_sniper.engine import quiet

DAY = datetime(2026, 9, 6)


def at(hhmm: str) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return DAY.replace(hour=hour, minute=minute)


def test_a_window_inside_one_day() -> None:
    window = quiet.parse("13:00-14:30")
    assert window is not None
    assert not window.contains(at("12:59"))
    assert window.contains(at("13:00"))
    assert window.contains(at("14:29"))
    assert not window.contains(at("14:30"))


def test_a_window_across_midnight() -> None:
    window = quiet.parse("23:00-07:00")
    assert window is not None
    assert window.contains(at("23:00"))
    assert window.contains(at("03:14"))
    assert window.contains(at("06:59"))
    assert not window.contains(at("07:00"))
    assert not window.contains(at("12:00"))


def test_blank_means_never_quiet() -> None:
    assert quiet.parse(None) is None
    assert quiet.parse("   ") is None
    assert not quiet.is_quiet("", at("03:00"))


@pytest.mark.parametrize("spec", ["23-07", "25:00-07:00", "23:00-23:00", "night", "23:00-07:60"])
def test_nonsense_is_rejected_at_input_time(spec: str) -> None:
    assert quiet.validate(spec) is not None
    assert not quiet.is_quiet(spec, at("03:00")), "and never silences anything by accident"


def test_spacing_is_forgiven() -> None:
    assert str(quiet.parse(" 9:5 - 17:00 ")) == "09:05-17:00"
