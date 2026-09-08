#!/usr/bin/env python
"""Drive one real, paid Magic Search sweep from /magic — and capture what it cost.

This is S06's acceptance runner. It does not reimplement the sweep: it drives the real
page in a real browser (map -> confirm -> run), reads the sweep id the page navigates to,
polls `GET /api/sweeps/{id}` until the run stops being `running`, and writes three things
to `data/acceptance/`: the raw API body, the run's own log lines, and a screenshot of the
finished results view. Going through the page rather than the engine is the point — that
is what puts S04's screen, S02's mapper and S01's engine on one thread.

Two modes, and only one of them can spend anything:

    .venv/bin/python dev/acceptance_run.py --verify-capture
        Reads data/acceptance/sweep.json and nothing else. No browser, no network, no
        money. This is the verify command.

    .venv/bin/python dev/acceptance_run.py --run --yes-spend-real-money
        The paid run. Refuses without that second flag, refuses if the preflight says
        NO-GO, and runs exactly one sweep. A failed run is never silently retried — the
        record has to say so.

Start the app yourself with its log going somewhere this can read, e.g.

    .venv/bin/python -m vinted_sniper.cli serve --log-format json \\
        > data/acceptance/app.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from vinted_sniper.config import Settings

ROOT = Path(__file__).resolve().parent.parent

OUT_DIR = ROOT / "data" / "acceptance"
SWEEP_JSON = OUT_DIR / "sweep.json"
RUN_LOG = OUT_DIR / "run.log"
SCREENSHOT = OUT_DIR / "results.png"
COUNTS_JSON = OUT_DIR / "table-counts.json"

#: The reference sentence the roadmap names. Kept here so the record and the run agree.
SENTENCE = "panska bunda Patagonia Torrentshell M do 60 eur"

#: Every line worth keeping out of the app's log. `sweep.cost` carries the five R004
#: fields, so grepping it is the whole cost report; the failure events are here because a
#: `partial` run is a legitimate outcome and the record has to show which stage degraded.
EVENTS = (
    "sweep.page",
    "sweep.judged",
    "sweep.cost",
    "magic.sweep_started",
    "magic.sweep_finished",
    "sweep.triage_failed",
    "sweep.verdict_failed",
    "sweep.blocked",
    "sweep.price_cap_unreadable",
)

#: A sweep that reached either of these has stopped. `partial` is a success for our
#: purposes: a judged sweep degrades rather than fails, keeping what was already paid for.
TERMINAL = ("ok", "partial", "error")
ACCEPTABLE = ("ok", "partial")


def _emit(line: str = "") -> None:
    print(line)


# --------------------------------------------------------------------------- capture


def select_log_lines(lines: list[str], sweep_id: int | None) -> list[str]:
    """Keep the lines this run produced, in order, and drop every other sweep's.

    Pure so it can be tested without an app: the caller supplies the log's lines. A line
    is kept when it names one of `EVENTS`. When it also carries a `sweep_id` and that id
    belongs to another run, it is dropped — otherwise a machine with six earlier sweeps
    in its log would produce a record claiming several `sweep.cost` lines.
    """
    kept: list[str] = []
    for line in lines:
        if not any(event in line for event in EVENTS):
            continue
        if sweep_id is not None and _other_sweep(line, sweep_id):
            continue
        kept.append(line.rstrip("\n"))
    return kept


def _other_sweep(line: str, sweep_id: int) -> bool:
    """True when the line names a sweep id that is not ours.

    Reads JSON when the line is JSON and falls back to the console renderer's
    `sweep_id=N`. A line with no sweep id at all is never "another sweep's" — the two
    `magic.*` events are logged before the id is bound.
    """
    try:
        found = json.loads(line).get("sweep_id")
    except (ValueError, AttributeError):
        marker = "sweep_id="
        if marker not in line:
            return False
        tail = line.split(marker, 1)[1]
        digits = ""
        for char in tail:
            if not char.isdigit():
                break
            digits += char
        found = int(digits) if digits else None
    return found is not None and found != sweep_id


def table_counts(db_path: str) -> dict[str, int]:
    """Row counts for the tables a sweep must not touch, plus the ones it owns.

    A sweep-written `items` row would make the standing poller treat that listing as
    already-seen and never alert on it, so the record shows this count unchanged either
    side of the run.
    """
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        for table in ("items", "queries", "sweep_runs", "sweep_candidates"):
            try:
                counts[table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = -1
    return counts


# ---------------------------------------------------------------------- verification


def _shown(path: Path) -> str:
    """Name a path relative to the repo when it is inside it, and plainly when it is not.

    `relative_to` raises for anything outside the checkout, and this is used only to build
    messages — a reader pointing at /tmp deserves a report, not a traceback.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class Check:
    ok: bool
    line: str

    def render(self) -> str:
        return f"  [{'ok' if self.ok else 'XX'}] {self.line}"


def verify_capture(path: Path = SWEEP_JSON) -> tuple[int, list[Check]]:
    """Read a captured sweep back and say whether it is a real terminal run.

    Spends nothing and touches nothing but this one file — that is why it is the verify
    command. Returns the exit code alongside the checks so a caller (and the test) can
    read the reasons rather than scrape stdout.
    """
    if not path.exists():
        return 1, [
            Check(False, f"{_shown(path)} is not there — no paid run has been captured."),
        ]

    try:
        body = json.loads(path.read_text())
    except ValueError as exc:
        return 1, [Check(False, f"{_shown(path)} is not readable JSON: {exc}")]
    if not isinstance(body, dict):
        return 1, [Check(False, "the capture is not one sweep object.")]

    checks = [Check(True, "the capture parses.")]

    status = body.get("status")
    checks.append(
        Check(
            status in ACCEPTABLE,
            f"the run finished as {status!r}"
            + ("." if status in ACCEPTABLE else f", which is not one of {ACCEPTABLE}."),
        )
    )

    for field in ("tokens", "cost_eur"):
        value = body.get(field)
        checks.append(
            Check(value is not None, f"{field} is {'missing' if value is None else value}.")
        )

    return (0 if all(check.ok for check in checks) else 1), checks


# ------------------------------------------------------------------------- the run


def _preflight_ok() -> bool:
    """Re-run the spend gate. The run refuses to start behind a NO-GO."""
    done = subprocess.run(
        [sys.executable, str(ROOT / "dev" / "acceptance_preflight.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    _emit(done.stdout.rstrip())
    return done.returncode == 0


def drive_sweep(base: str, tld: str, timeout: float) -> int:
    """Type the sentence into /magic, confirm, run, and return the sweep id.

    Playwright is imported here rather than at module scope so `--verify-capture` keeps
    working on a machine with no browser installed.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            page.goto(f"{base}/magic", wait_until="domcontentloaded")
            page.fill("#m-text", SENTENCE)
            page.select_option("#m-tld", tld)
            page.click("#m-map")

            page.wait_for_selector("#m-step-confirm:not([hidden])", timeout=timeout * 1000)
            confirm = {
                "labels": page.inner_text("#m-labels"),
                "signature": page.inner_text("#m-signature"),
                "hints": page.inner_text("#m-hints"),
                # S05 moved the ceiling sentence above the button that spends the money.
                "ceiling_above_run": page.inner_text("#m-step-confirm"),
            }
            (OUT_DIR / "confirm-card.json").write_text(json.dumps(confirm, indent=2) + "\n")
            _emit("  the confirm card was captured; clicking the run button once.")

            page.click("#m-run")
            sweep_id = _await_sweep_id(page, timeout)
            _emit(f"  the page started sweep {sweep_id}.")
            _poll_until_terminal(base, sweep_id, timeout)
            page.screenshot(path=str(SCREENSHOT), full_page=True)
            return sweep_id
        finally:
            browser.close()


def _await_sweep_id(page: object, timeout: float) -> int:
    """Read the id from `#m-results[data-sweep-id]`, or from the ?sweep= the page goes to."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = page.get_attribute("#m-results", "data-sweep-id")  # type: ignore[attr-defined]
        if found:
            return int(found)
        url = page.url  # type: ignore[attr-defined]
        if "sweep=" in url:
            return int(url.split("sweep=", 1)[1].split("&", 1)[0])
        page.wait_for_timeout(500)  # type: ignore[attr-defined]
    raise TimeoutError("the page never published a sweep id")


def _poll_until_terminal(base: str, sweep_id: int, timeout: float) -> dict[str, object]:
    """Poll the real API until the run stops being `running`, then write it down raw."""
    deadline = time.monotonic() + max(timeout, 600.0)
    while time.monotonic() < deadline:
        reply = httpx.get(f"{base}/api/sweeps/{sweep_id}", timeout=30.0)
        reply.raise_for_status()
        body: dict[str, object] = reply.json()
        if body.get("status") in TERMINAL:
            # Written exactly as served, unedited — that is the evidence.
            SWEEP_JSON.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            return body
        time.sleep(5)
    raise TimeoutError(f"sweep {sweep_id} was still running after {deadline:.0f}s")


def do_run(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _emit("Checking the spend gate before anything is billed:")
    if not _preflight_ok():
        _emit()
        _emit("Refusing to run: the preflight says NO-GO. Nothing was spent.")
        return 1

    db_path = Settings().db_path
    before = table_counts(db_path)
    _emit(f"  before: {before}")

    sweep_id = drive_sweep(args.base_url, args.tld, args.timeout)

    after = table_counts(db_path)
    COUNTS_JSON.write_text(
        json.dumps({"before": before, "after": after, "sweep_id": sweep_id}, indent=2) + "\n"
    )
    _emit(f"  after:  {after}")

    app_log = Path(args.app_log)
    if app_log.exists():
        kept = select_log_lines(app_log.read_text(errors="replace").splitlines(), sweep_id)
        RUN_LOG.write_text("\n".join(kept) + "\n")
        _emit(f"  kept {len(kept)} log lines for sweep {sweep_id}.")
    else:
        _emit(f"  no app log at {app_log} — run.log was not written.")

    code, checks = verify_capture()
    for check in checks:
        _emit(check.render())
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verify-capture",
        action="store_true",
        help="read the captured sweep and check it; spends nothing",
    )
    parser.add_argument("--run", action="store_true", help="do the paid run")
    parser.add_argument(
        "--yes-spend-real-money",
        action="store_true",
        help="required by --run; without it the run refuses to start",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tld", default="sk")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--app-log", default=str(OUT_DIR / "app.log"))
    args = parser.parse_args(argv)

    if args.verify_capture and args.run:
        _emit("Pick one: --verify-capture reads, --run spends.")
        return 2

    if args.run:
        if not args.yes_spend_real_money:
            _emit("Refusing: --run bills real money, so it needs --yes-spend-real-money too.")
            return 2
        return do_run(args)

    if not args.verify_capture:
        parser.print_help()
        return 2

    code, checks = verify_capture()
    _emit("The captured sweep:")
    for check in checks:
        _emit(check.render())
    _emit()
    _emit("CAPTURE OK" if code == 0 else "NO CAPTURE")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
