#!/usr/bin/env python
"""Say whether a real, paid Magic Search sweep can be attempted here — before it spends.

The acceptance run in S06 pays real money against real Vinted and the real n8n flows. A
run started blind either dies halfway through having already paid for a photo-check
batch, or — worse — finishes looking successful with zeroes in it. This script is the
gate in front of that: it reports one readiness line per precondition and ends with a
single `GO` or `NO-GO`, exiting 0 or 1 to match.

Nothing secret is ever printed. Flows are reported as configured yes/no plus, when
configured, whether the endpoint answered and with which status code. Every line goes
through a scrubber that blanks anything shaped like a URL, so a webhook address cannot
escape even inside an error message from the HTTP client.

    .venv/bin/python dev/acceptance_preflight.py
    .venv/bin/python dev/acceptance_preflight.py --timeout 10
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
from pydantic import ValidationError

from vinted_sniper.config import Settings

# Anything URL-shaped is blanked on the way out. The probe deliberately reports only
# booleans and status codes, but httpx exceptions and pydantic validation errors quote
# their input, and this file's whole promise is that neither can leak a webhook address.
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

_DOCS = "docs/magic-search.md"

_HTTP_OK = 200
_HTTP_CLIENT_ERROR = 400
_HTTP_NOT_FOUND = 404


def _scrub(text: str) -> str:
    return _URL.sub("<url hidden>", text)


def _emit(line: str = "") -> None:
    print(_scrub(line))


@dataclass(frozen=True)
class Probe:
    """What one HTTP probe learned, with nothing in it that could identify the address."""

    reachable: bool
    status: int | None = None
    #: True when n8n served this path for GET, False when it answered "not registered",
    #: None when the answer came from something that is not n8n.
    get_registered: bool | None = None
    #: Exception class name only — never the message, which quotes the URL.
    error: str | None = None

    def describe(self) -> str:
        if not self.reachable:
            return f"no answer ({self.error})"
        parts = [f"answered HTTP {self.status}"]
        if self.get_registered is False:
            parts.append("n8n has no GET handler on that path")
        elif self.get_registered is True:
            parts.append("n8n served it")
        return ", ".join(parts)


@dataclass(frozen=True)
class Check:
    """One readiness line. `ok` is what decides GO; `detail` is what an operator reads."""

    label: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"  [{'ok' if self.ok else 'XX'}] {self.label}: {self.detail}"


def probe(url: str, timeout_s: float) -> Probe:
    """Ask the endpoint one free question: a GET, which never runs a POST-only flow.

    This cannot prove a flow is published — n8n registers a webhook per method, so a
    normal POST-only production flow answers a GET with "not registered" exactly like a
    path that does not exist at all. What it does prove is that n8n itself is up and
    reachable from here, which is the failure this catches before spending.
    """
    try:
        response = httpx.get(url, timeout=timeout_s, follow_redirects=False)
    except httpx.HTTPError as exc:
        return Probe(reachable=False, error=type(exc).__name__)
    body = response.text[:2000] if response.content else ""
    if "is not registered" in body:
        registered: bool | None = False
    elif "n8n" in body.lower() or response.status_code < _HTTP_CLIENT_ERROR:
        registered = True
    else:
        registered = None
    return Probe(reachable=True, status=response.status_code, get_registered=registered)


def probe_page(url: str, timeout_s: float) -> Probe:
    """Same free question for the dashboard, where a 200 is the whole point.

    A 404 here is the stale-deployment trap: an image built before the Magic Search
    pages existed still serves `/healthz` happily and 404s on `/magic`.
    """
    try:
        response = httpx.get(url, timeout=timeout_s, follow_redirects=True)
    except httpx.HTTPError as exc:
        return Probe(reachable=False, error=type(exc).__name__)
    return Probe(reachable=True, status=response.status_code)


FLOWS: tuple[tuple[str, str, str], ...] = (
    ("magic_webhook_url", "the mapper flow (plain words to filters)", "L47-105"),
    ("magic_triage_webhook_url", "the photo check flow", "L263-336"),
    ("magic_verdict_webhook_url", "the full opinion flow", "L337-421"),
)


def flow_checks(settings: Settings, timeout_s: float, allow_network: bool) -> list[Check]:
    checks: list[Check] = []
    for field, human, _ in FLOWS:
        url = getattr(settings, field)
        if not url:
            checks.append(Check(human, False, "not configured"))
            continue
        if not allow_network:
            checks.append(Check(human, False, "configured, not probed"))
            continue
        result = probe(url, timeout_s)
        checks.append(Check(human, result.reachable, f"configured, {result.describe()}"))
    return checks


def dashboard_checks(base: str | None, timeout_s: float, allow_network: bool) -> list[Check]:
    if base is None:
        return [Check("the dashboard", False, "the web UI is switched off")]
    if not allow_network:
        return [Check("the dashboard", False, "not probed")]
    checks: list[Check] = []
    pages = (
        ("/healthz", "the dashboard answers"),
        ("/magic", "the dashboard serves Magic Search"),
    )
    for path, label in pages:
        result = probe_page(f"{base.rstrip('/')}{path}", timeout_s)
        ok = result.reachable and result.status == _HTTP_OK
        detail = result.describe()
        if result.status == _HTTP_NOT_FOUND:
            detail += " — this build predates the page"
        checks.append(Check(label, ok, detail))
    return checks


def ceilings(settings: Settings) -> list[str]:
    """The settings that decide the size of the bill, printed so the record has them."""
    return [
        f"  pages read per sweep:     {settings.sweep_max_pages}",
        f"  listings looked at:       {settings.sweep_max_items}",
        f"  photo check batch:        {settings.sweep_triage_batch}",
        f"  full opinions paid for:   {settings.sweep_max_verdicts}",
        f"  cost per million in:      {settings.magic_cost_per_mtok_in} EUR",
        f"  cost per million out:     {settings.magic_cost_per_mtok_out} EUR",
        f"  database:                 {settings.db_path}",
    ]


def remediation(settings: Settings, checks: list[Check]) -> list[str]:
    """What an operator has to go and do, naming only what is actually missing."""
    lines = ["What has to happen before this run can be attempted:", ""]
    for field, human, anchor in FLOWS:
        if getattr(settings, field):
            continue
        lines.append(f"  - {human} is unset. Its contract is in {_DOCS} ({anchor}).")
    if settings.magic_verdict_webhook_url is None:
        lines.append(
            f"    The full opinion flow is the cheapest one to stand up: {_DOCS} says to "
            "point it at a copy of the enrichment flow, and magic/verdict.py reads that "
            "flow's answer shape unchanged, so an untouched copy already answers it."
        )
    if any(not check.ok for check in checks if check.label.startswith("the dashboard")):
        lines.append(
            "  - the dashboard is not serving /magic on this machine. The run is driven "
            "from that page, so start the app locally, or update the deployed image if "
            "the page 404s there."
        )
    lines += [
        "",
        "Two traps this project has hit before:",
        "  - importing a workflow into n8n leaves it deactivated. Republish it after "
        "importing, or every call answers 'not registered'.",
        f"  - when a judging flow answers but the sweep still finds nothing, {_DOCS} "
        "(L468-488) is the failure-triage section that says which stage to look at.",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for each probe")
    parser.add_argument(
        "--dashboard-url",
        default=None,
        help="where the dashboard is; defaults to the configured address",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError as exc:  # pydantic quotes its input, so scrub the report
        _emit("Readiness for a paid sweep:")
        _emit(f"  [XX] the settings do not load: {type(exc).__name__}")
        _emit(f"       {exc}")
        _emit()
        _emit("NO-GO")
        return 1

    configured = [field for field, _, _ in FLOWS if getattr(settings, field)]
    # With nothing configured there is nothing a network call could tell us, and the
    # answer is already NO-GO — so do not make one. That keeps the unconfigured case
    # instant, which is also the case the test exercises.
    allow_network = bool(configured)

    checks = flow_checks(settings, args.timeout, allow_network)
    base = args.dashboard_url if args.dashboard_url is not None else settings.dashboard_url
    checks += dashboard_checks(base, args.timeout, allow_network)

    _emit("Readiness for a paid sweep:")
    for check in checks:
        _emit(check.render())
    if not allow_network:
        _emit("  (nothing was probed: no flow is configured, so the answer is already no.)")
    _emit()
    _emit("What one sweep is allowed to spend:")
    for line in ceilings(settings):
        _emit(line)
    _emit()

    go = all(check.ok for check in checks)
    if not go:
        for line in remediation(settings, checks):
            _emit(line)
        _emit()
        _emit("NO-GO")
        return 1

    _emit("A GET cannot prove a POST-only flow is published; the sweep itself is that proof.")
    _emit("GO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
