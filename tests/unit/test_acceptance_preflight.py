"""The preflight must refuse an unconfigured checkout, say why, and leak nothing.

The script is run as a real subprocess rather than imported: exiting non-zero is half of
what it promises, and only a subprocess can show that. The three magic URL variables are
cleared and the process is started in an empty directory so that neither the ambient
environment nor a developer's own `.env` (pydantic-settings reads it relative to the
working directory) can turn this into a GO and quietly weaken the test.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "dev" / "acceptance_preflight.py"

MAGIC_URL_VARS = (
    "VINTED_SNIPER_MAGIC_WEBHOOK_URL",
    "VINTED_SNIPER_MAGIC_TRIAGE_WEBHOOK_URL",
    "VINTED_SNIPER_MAGIC_VERDICT_WEBHOOK_URL",
)


def run_preflight(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in MAGIC_URL_VARS}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )


def load_script() -> ModuleType:
    """Import the script by path: `dev/` is a runner directory, not an installed package."""
    spec = importlib.util.spec_from_file_location("acceptance_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its field annotations through `sys.modules[cls.__module__]`,
    # so a module executed without being registered there fails on its first dataclass.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_unconfigured_checkout_is_a_refusal(tmp_path: Path) -> None:
    result = run_preflight(tmp_path)

    assert result.returncode != 0, result.stdout
    assert "NO-GO" in result.stdout
    # The verdict is the last thing printed, and it is the refusal — not a GO that a
    # later line contradicts. ("NO-GO" ends in "GO", so this has to be an exact match.)
    assert result.stdout.strip().splitlines()[-1] == "NO-GO"


def test_refusal_names_every_missing_flow(tmp_path: Path) -> None:
    stdout = run_preflight(tmp_path).stdout

    assert "the mapper flow" in stdout
    assert "the photo check flow" in stdout
    assert "the full opinion flow" in stdout
    assert stdout.count("not configured") == 3


def test_refusal_carries_a_remediation_block(tmp_path: Path) -> None:
    stdout = run_preflight(tmp_path).stdout

    assert "What has to happen before this run can be attempted:" in stdout
    assert stdout.count("docs/magic-search.md") >= 2
    # The two traps this project has actually hit, named where an operator will read them.
    assert "deactivated" in stdout
    assert "copy of the enrichment flow" in stdout


def test_refusal_prints_the_ceilings_that_decide_the_bill(tmp_path: Path) -> None:
    stdout = run_preflight(tmp_path).stdout

    for label in (
        "pages read per sweep:",
        "listings looked at:",
        "photo check batch:",
        "full opinions paid for:",
        "cost per million in:",
        "cost per million out:",
        "database:",
    ):
        assert label in stdout


def test_refusal_leaks_no_url(tmp_path: Path) -> None:
    """The leak guard: booleans, status codes and integers only — never an address."""
    result = run_preflight(tmp_path)

    assert "http://" not in result.stdout
    assert "https://" not in result.stdout
    assert "http://" not in result.stderr
    assert "https://" not in result.stderr


def test_nothing_configured_short_circuits_before_any_network_call(tmp_path: Path) -> None:
    """No flow configured means no probe — the refusal is decidable without the network."""
    stdout = run_preflight(tmp_path).stdout

    assert "nothing was probed" in stdout
    assert "answered HTTP" not in stdout


def test_the_scrubber_blanks_a_url_inside_arbitrary_text() -> None:
    """The guard is structural, so an error message quoting a webhook cannot escape."""
    _scrub = load_script()._scrub

    assert "example.test" not in _scrub("failed for https://n8n.example.test/webhook/abc")
    assert _scrub("no address here") == "no address here"
