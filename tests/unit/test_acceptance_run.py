"""The acceptance runner must refuse to spend by accident and must not bless a bad capture.

The paid half of this script cannot be exercised offline — it drives a browser against a
running app and bills two n8n flows. What is tested here is everything guarding it: the
opt-in flag, the capture reader that decides whether a run counts, and the log filter
that has to produce exactly one `sweep.cost` line on a machine whose log already holds
six earlier sweeps.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "dev" / "acceptance_run.py"


def load_script() -> ModuleType:
    """Import the script by path: `dev/` is a runner directory, not an installed package."""
    spec = importlib.util.spec_from_file_location("acceptance_run", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its annotations through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script()


def capture(**overrides: object) -> dict[str, object]:
    body = {"id": 7, "status": "ok", "tokens": 4210, "cost_eur": 0.0163}
    body.update(overrides)
    return body


def write(tmp_path: Path, body: object) -> Path:
    path = tmp_path / "sweep.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


# ----------------------------------------------------------------- the spend guard


def test_run_without_the_opt_in_flag_refuses(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """--run alone must not reach the browser, the app or a webhook."""
    code = script.main(["--run"])

    assert code == 2
    assert "yes-spend-real-money" in capsys.readouterr().out


def test_run_and_verify_together_is_refused(script: ModuleType) -> None:
    """The read-only mode and the paying mode cannot both be asked for at once."""
    assert script.main(["--verify-capture", "--run"]) == 2


def test_no_mode_prints_help_rather_than_spending(script: ModuleType) -> None:
    assert script.main([]) == 2


# -------------------------------------------------------------- the capture reader


def test_a_real_terminal_capture_passes(script: ModuleType, tmp_path: Path) -> None:
    code, checks = script.verify_capture(write(tmp_path, capture()))

    assert code == 0
    assert all(check.ok for check in checks)


def test_a_partial_run_is_still_acceptable(script: ModuleType, tmp_path: Path) -> None:
    """A judged sweep degrades rather than fails, keeping what was already paid for."""
    code, _ = script.verify_capture(write(tmp_path, capture(status="partial")))

    assert code == 0


def test_a_missing_capture_is_not_a_pass(script: ModuleType, tmp_path: Path) -> None:
    code, checks = script.verify_capture(tmp_path / "absent.json")

    assert code == 1
    assert not checks[0].ok


def test_unparseable_capture_is_not_a_pass(script: ModuleType, tmp_path: Path) -> None:
    code, _ = script.verify_capture(write(tmp_path, "{not json"))

    assert code == 1


def test_a_json_list_is_not_one_sweep(script: ModuleType, tmp_path: Path) -> None:
    code, _ = script.verify_capture(write(tmp_path, [capture()]))

    assert code == 1


@pytest.mark.parametrize("status", ["running", "error", None])
def test_a_non_terminal_or_failed_run_is_not_a_pass(
    script: ModuleType, tmp_path: Path, status: str | None
) -> None:
    """`running` would mean the poll gave up early; `error` means nothing was delivered."""
    code, _ = script.verify_capture(write(tmp_path, capture(status=status)))

    assert code == 1


@pytest.mark.parametrize("field", ["tokens", "cost_eur"])
def test_a_capture_without_the_money_fields_is_not_a_pass(
    script: ModuleType, tmp_path: Path, field: str
) -> None:
    """The whole point of the run is the bill; a capture missing it proves nothing."""
    code, _ = script.verify_capture(write(tmp_path, capture(**{field: None})))

    assert code == 1


def test_a_zero_cost_capture_still_reports_its_numbers(script: ModuleType, tmp_path: Path) -> None:
    """0 is a real reading, not a missing one — the reader must not confuse them."""
    code, checks = script.verify_capture(write(tmp_path, capture(tokens=0, cost_eur=0.0)))

    assert code == 0
    assert any("tokens is 0" in check.line for check in checks)


# ------------------------------------------------------------------ the log filter


def test_only_this_sweep_s_lines_are_kept(script: ModuleType) -> None:
    """Six earlier sweeps in the same log must not become six cost lines in the record."""
    lines = [
        json.dumps({"event": "sweep.cost", "sweep_id": 3, "tokens": 99}),
        json.dumps({"event": "sweep.cost", "sweep_id": 7, "tokens": 4210}),
        json.dumps({"event": "sweep.judged", "sweep_id": 7, "matched": 5}),
        json.dumps({"event": "poll.tick", "sweep_id": 7}),
    ]

    kept = script.select_log_lines(lines, 7)

    assert len(kept) == 2
    assert sum("sweep.cost" in line for line in kept) == 1
    assert not any("poll.tick" in line for line in kept)


def test_lines_logged_before_the_id_is_bound_are_kept(script: ModuleType) -> None:
    """`magic.sweep_started` is logged before the sweep id exists, so it carries none."""
    lines = [json.dumps({"event": "magic.sweep_started", "text": "..."})]

    assert script.select_log_lines(lines, 7) == lines


def test_the_console_renderer_is_filtered_too(script: ModuleType) -> None:
    """A developer who forgot --log-format json still gets a correct record."""
    lines = [
        "2026-09-08T10:00:00Z [info] sweep.cost sweep_id=3 tokens=99",
        "2026-09-08T10:00:01Z [info] sweep.cost sweep_id=7 tokens=4210",
    ]

    kept = script.select_log_lines(lines, 7)

    assert len(kept) == 1
    assert "sweep_id=7" in kept[0]


# -------------------------------------------------------------- reading the sweep id


class FakePage:
    """A page that behaves the way `/magic` really does during a run.

    `#m-results` is absent for the whole sweep — `magic.html` renders that section only
    for a `?sweep=N` request — and the address only gains `?sweep=N` when the browser is
    redirected on the last poll. A reader that asks for the attribute first, on a page
    object that blocks while a selector is missing, therefore never gets to look at the
    address at all. That is the failure this pins.
    """

    def __init__(self, redirect_after: int) -> None:
        self._redirect_after = redirect_after
        self.ticks = 0
        self.selector_asks = 0

    @property
    def url(self) -> str:
        return "http://x/magic?sweep=41" if self.ticks >= self._redirect_after else "http://x/magic"

    def query_selector(self, _selector: str) -> None:
        self.selector_asks += 1

    def wait_for_timeout(self, _ms: int) -> None:
        self.ticks += 1


def test_the_sweep_id_is_read_from_the_redirect_the_page_makes_when_it_finishes(
    script: ModuleType,
) -> None:
    page = FakePage(redirect_after=3)
    assert script._await_sweep_id(page, timeout=5.0) == 41


def test_waiting_for_the_id_outlasts_a_short_timeout_because_it_waits_for_the_sweep(
    script: ModuleType,
) -> None:
    # A sweep takes minutes; --timeout is the page-render budget. Reading the id has to
    # use the longer of the two or the runner abandons a sweep it has already paid for.
    page = FakePage(redirect_after=200)
    assert script._await_sweep_id(page, timeout=1.0) == 41
