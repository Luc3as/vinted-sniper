"""Process settings, read once from the environment at startup.

Everything a *user* owns — searches, destinations, routing, per-search filters — lives in
SQLite instead, managed through the CLI or web UI. Keeping those two apart means there is
never a question of which copy of a setting wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Not configurable. Polling faster than this earns 403s without finding items any sooner,
# because Vinted's own catalog lags behind uploads by far more than a few seconds.
MIN_POLL_INTERVAL_S = 10

# Ceiling for the 403 backoff ladder.
MAX_BACKOFF_S = 900


def _in_container() -> bool:
    """Whether we are inside Docker or Podman.

    There, 0.0.0.0 is the only bind that works with published ports, and how far the
    dashboard is exposed is decided by the port mapping, which the app cannot see. The
    shipped docker-compose.yml publishes to the host's loopback only.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


class Settings(BaseSettings):
    """Static configuration. Every field is documented in docs/configuration.md."""

    model_config = SettingsConfigDict(
        env_prefix="VINTED_SNIPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # --- Storage -------------------------------------------------------------------
    db_path: Path = Field(
        default=Path("./data/app.db"),
        description="Path to the SQLite database file. Its parent directory is created on start.",
    )
    item_retention_days: int = Field(
        default=30,
        ge=1,
        description="Delete stored items older than this. Does not cause re-notification.",
    )
    keep_raw_json: bool = Field(
        default=False,
        description="Store each item's raw API payload. Useful for debugging schema drift, "
        "but it keeps more seller data on disk than notifications need.",
    )

    # --- Logging -------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_color: bool = Field(
        default=False,
        description="ANSI colours in console logs. Off by default: Portainer's and Docker's "
        "log viewers render the escape codes as garbled, repeated words.",
    )

    # --- Vinted transport ----------------------------------------------------------
    http_impersonate: bool = Field(
        default=False,
        description="Route Vinted requests through curl_cffi with a browser TLS fingerprint. "
        "Requires the 'impersonate' extra. Only needed if plain requests start getting 403s.",
    )
    proxy_file: Path | None = Field(
        default=None,
        description="Optional text file of proxy URLs, one per line. Off by default.",
    )
    session_rotate_minutes: int = Field(
        default=60,
        ge=1,
        description="Discard and re-bootstrap a site session once it reaches this age. "
        "Blocks correlate with session age more than with request rate.",
    )
    request_timeout_s: float = Field(default=15.0, gt=0)
    site_requests_per_minute: int = Field(
        default=12,
        ge=1,
        description="Upper bound on requests to any one country site from this address, "
        "counted across every search and including homepage loads. The address is what "
        "gets scored, so the total matters more than any single search's interval.",
    )
    startup_stagger_s: float = Field(
        default=20.0,
        ge=0,
        description="Seconds between the first checks of successive searches at startup, "
        "so ten searches do not all introduce themselves in the same second.",
    )

    # --- Polling -------------------------------------------------------------------
    poll_default_interval_s: int = Field(
        default=60,
        ge=MIN_POLL_INTERVAL_S,
        description="Default seconds between checks for a new search. Per-search overrides "
        "live in the database.",
    )
    freshness_window_min: int = Field(
        default=20,
        ge=1,
        description="Ignore listings whose photo timestamp is older than this. Stops a restart "
        "or a slow first poll from replaying yesterday's catalog.",
    )
    price_drop_min_percent: int = Field(
        default=10,
        ge=0,
        le=100,
        description="Announce a listing again when its total price falls by at least this "
        "much since we recorded it. Costs no extra requests: only listings still on the "
        "search's first page are compared. 0 turns it off.",
    )
    first_run_mode: Literal["silent", "newest"] = Field(
        default="silent",
        description="What a brand-new search does on its first check: 'silent' notifies "
        "nothing, 'newest' sends exactly one item so you can confirm delivery works.",
    )

    # --- Watchdog ------------------------------------------------------------------
    watchdog_stale_cycles: int = Field(
        default=10,
        ge=2,
        description="Consecutive checks with no newer listing before a search is called stale.",
    )
    watchdog_action: Literal["warn", "rotate"] = Field(
        default="rotate",
        description="What to do about a stale search: log a warning, or also force a new session.",
    )

    # --- Enrichment ----------------------------------------------------------------
    enrichment_wait_s: int = Field(
        default=0,
        ge=0,
        le=600,
        description="Hold a listing's chat notifications this long so an outside agent "
        "(fed by a webhook destination) can post a verdict to /api/items/{id}/enrichment "
        "first. 0 turns the loop off. Webhook destinations are never held.",
    )
    enrichment_highlight_score: int = Field(
        default=75,
        ge=0,
        le=100,
        description="A deal score at or above this is headlined as a hot deal.",
    )
    enrichment_silent_below: int = Field(
        default=40,
        ge=0,
        le=100,
        description="A deal score below this, or a verdict that the listing is not the "
        "product searched for, is delivered without a notification sound where the "
        "platform allows it (Telegram).",
    )

    # --- Magic Search ----------------------------------------------------------------
    sweep_max_pages: int = Field(
        default=4,
        ge=1,
        le=10,
        description="How many pages of listings already on Vinted one sweep reads, best "
        "matches first. More pages reach further back, but every page is another request.",
    )
    sweep_max_items: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="The most listings one sweep will look at, counted before anything is "
        "sent to the AI. A hard stop, whatever the page count would otherwise allow.",
    )
    magic_webhook_url: str | None = Field(
        default=None,
        description="The n8n flow that turns plain words like 'men's Patagonia jacket size M "
        "under 60 eur' into the filters a search needs. Leave it unset and Magic Search is "
        "off: the endpoint says so instead of guessing.",
    )
    magic_webhook_token: SecretStr | None = Field(
        default=None,
        description="Sent to that flow as a bearer token, so a stranger who finds the URL "
        "cannot use it. Set it only if the flow asks for one.",
    )
    magic_timeout_s: float = Field(
        default=30.0,
        gt=0,
        le=120,
        description="How long to wait for the flow to answer before giving up. An AI "
        "reading a sentence takes a few seconds; a minute means something is wrong.",
    )

    # --- Delivery ------------------------------------------------------------------
    outbox_expiry_minutes: int = Field(
        default=60,
        ge=1,
        description="Give up on an undelivered notification after this long. A two-hour-old "
        "listing alert is not worth sending.",
    )
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        description="Enables Telegram delivery and the /start binding bot when set.",
    )
    weekly_report: bool = Field(
        default=True,
        description="Send a short weekly summary (listings found, price drops, hottest "
        "verdicts, busiest searches) to destinations flagged for status notices, on "
        "Monday at 08:00 in TIMEZONE.",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA timezone (e.g. Europe/Bratislava) that a destination's quiet hours "
        "are read in.",
    )

    @field_validator("timezone")
    @classmethod
    def _timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    # --- Development ---------------------------------------------------------------
    fetch_mode: Literal["live", "mock"] = Field(
        default="live",
        description="'mock' replays recorded responses from disk instead of calling Vinted.",
    )
    mock_scenario_dir: Path | None = None

    # --- Web UI --------------------------------------------------------------------
    web_enabled: bool = True
    web_host: str = Field(
        default="127.0.0.1",
        description="Loopback by default. Only widen this behind a reverse proxy you trust.",
    )
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_auth_token: SecretStr | None = Field(
        default=None,
        description="Optional. When set, the dashboard asks for it as a password. Set it "
        "before exposing the dashboard beyond localhost: it shows your webhook URLs.",
    )
    web_public_url: str | None = Field(
        default=None,
        description="The address the dashboard is reachable at from wherever you read your "
        "alerts — set it when the dashboard sits behind a reverse proxy or a tunnel. It "
        "becomes the Dashboard link in Discord messages. Unset, that link points at "
        "http://<web_host>:<web_port>.",
    )

    @property
    def dashboard_url(self) -> str | None:
        """Where a notification may link the dashboard, or None while the web UI is off."""
        if not self.web_enabled:
            return None
        if self.web_public_url:
            return self.web_public_url.rstrip("/")
        # A wildcard bind is an instruction to the server, not an address a reader's
        # browser can open; loopback is the only honest guess left.
        host = "127.0.0.1" if self.web_host in {"0.0.0.0", "::"} else self.web_host
        return f"http://{host}:{self.web_port}"

    @property
    def web_is_loopback(self) -> bool:
        """Whether the dashboard is reachable only from this machine."""
        return self.web_host in {"127.0.0.1", "localhost", "::1"}

    @model_validator(mode="after")
    def _check_coherent(self) -> Settings:
        if self.fetch_mode == "mock" and self.mock_scenario_dir is None:
            raise ValueError(
                "VINTED_SNIPER_FETCH_MODE is 'mock' but VINTED_SNIPER_MOCK_SCENARIO_DIR is not set."
            )
        if self.sweep_max_items < self.sweep_max_pages:
            # A ceiling smaller than the page count stops the sweep before it has read a
            # single page through, which makes asking for several pages meaningless.
            raise ValueError(
                f"VINTED_SNIPER_SWEEP_MAX_ITEMS ({self.sweep_max_items}) is below "
                f"VINTED_SNIPER_SWEEP_MAX_PAGES ({self.sweep_max_pages}); a sweep cannot "
                "read fewer listings than the pages it is asked to fetch."
            )
        return self

    @property
    def web_is_exposed_without_a_password(self) -> bool:
        """Whether the dashboard could be reached from another machine with no sign-in.

        Not treated as fatal: refusing would stop `docker compose up` working at all. And
        inside a container this stays quiet, because 0.0.0.0 is the only bind that works
        there and the port mapping — which the app cannot see — is what actually decides
        who can reach it.
        """
        if not self.web_enabled or self.web_auth_token is not None:
            return False
        return not self.web_is_loopback and not _in_container()
