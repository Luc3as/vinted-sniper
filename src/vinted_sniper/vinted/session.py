"""Anonymous sessions.

Vinted hands a session cookie to anyone who loads the site. That cookie is all the catalog
API asks for, which is why this app never logs in and never touches an account: there is no
password to store, nothing to get locked out of, and nothing for Vinted to sanction beyond
an address.

Sessions are kept in the database so a restart does not mean a fresh handshake with every
country site, and are replaced on a timer because refusals track how long a session has
been alive more than how fast it is used.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from http import HTTPStatus

from vinted_sniper.db import Database
from vinted_sniper.log import get_logger
from vinted_sniper.vinted import headers as hdr
from vinted_sniper.vinted import urls
from vinted_sniper.vinted.errors import BlockedError, NetworkError
from vinted_sniper.vinted.pacing import RequestBudget, SiteCooldown
from vinted_sniper.vinted.proxies import QUARANTINE_BLOCKED_S, QUARANTINE_NETWORK_S, ProxyRotation
from vinted_sniper.vinted.transport import Transport, TransportError, TransportPool

log = get_logger(__name__)

# The cookie the catalog API actually checks. We keep the whole jar anyway — the site sets
# others that travel with it, and dropping them makes the request look assembled by hand.
SESSION_COOKIE = "access_token_web"


@dataclass(slots=True)
class Session:
    """One country site's anonymous session."""

    tld: str
    cookies: dict[str, str]
    identity: hdr.BrowserIdentity
    created_at: int
    request_count: int = 0
    # The route this session's cookie was minted through. Replaying it from somewhere else
    # is the kind of inconsistency anti-bot systems look for, so it travels with it.
    proxy: str | None = None

    def age_s(self, now: int | None = None) -> int:
        return (now if now is not None else int(time.time())) - self.created_at

    @property
    def cookie_header(self) -> dict[str, str]:
        return dict(self.cookies)


class SessionManager:
    """Creates, stores and replaces anonymous sessions, one per country site."""

    def __init__(
        self,
        db: Database,
        transport: Transport | TransportPool,
        *,
        rotate_after_minutes: int = 60,
        proxies: ProxyRotation | None = None,
        budget: RequestBudget | None = None,
        cooldown: SiteCooldown | None = None,
        impersonate: bool = False,
    ) -> None:
        self._db = db
        self._pool: TransportPool | None = None
        self._transport: Transport | None = None
        if isinstance(transport, TransportPool):
            self._pool = transport
        else:
            self._transport = transport
        self._rotate_after_s = rotate_after_minutes * 60
        # With TLS impersonation on, every request shakes hands like Chrome, so the
        # personas we claim must be Chromium ones — see `hdr.pick_identity`.
        self._impersonate = impersonate
        self._proxies = proxies or ProxyRotation()
        self._budget = budget
        self.cooldown = cooldown or SiteCooldown()
        self._cache: dict[str, Session] = {}
        # One bootstrap per site at a time. Without this, every search that starts on an
        # empty cache loads the homepage itself — a burst of different browser personas
        # from one address inside a second, which is the most bot-shaped thing this app
        # could possibly do.
        self._locks: dict[str, asyncio.Lock] = {}

    def transport_for(self, session: Session) -> Transport:
        """The client that reaches Vinted the same way this session was created."""
        if self._pool is not None:
            return self._pool.get(session.proxy)
        if self._transport is None:  # pragma: no cover - one of the two is always set
            raise RuntimeError("SessionManager was built without a transport")
        return self._transport

    async def get(self, tld: str) -> Session:
        """Return a usable session for a site, creating or replacing it as needed."""
        lock = self._locks.setdefault(tld, asyncio.Lock())
        async with lock:
            return await self._get_locked(tld)

    async def _get_locked(self, tld: str) -> Session:
        session = self._cache.get(tld) or await self._load(tld)

        if session is not None and session.age_s() >= self._rotate_after_s:
            log.info("session.expired", tld=tld, age_s=session.age_s())
            session = None

        if session is None:
            session = await self.bootstrap(tld)

        self._cache[tld] = session
        return session

    async def invalidate(self, tld: str) -> None:
        """Drop a session so the next call starts a new one."""
        self._cache.pop(tld, None)
        await self._db.execute("DELETE FROM sessions WHERE tld = ?", (tld,))

    async def discard_blocked(self, tld: str) -> None:
        """Drop a refused session without asking for another one yet.

        The route it came through is set aside too: a new cookie down the same blocked
        path buys nothing. The replacement is deliberately *not* fetched here — the caller
        is about to back off, and the next check will bootstrap when it is time. Asking
        again immediately after a refusal is the one move guaranteed to make it longer.
        """
        previous = self._cache.get(tld)
        await self.invalidate(tld)
        if previous is not None:
            self._proxies.bench(previous.proxy, QUARANTINE_BLOCKED_S, "refused by Vinted")

    async def rotate(self, tld: str, *, blocked: bool = False) -> Session:
        """Replace a session outright, with a different browser persona.

        When the old one was refused rather than merely old, its route is set aside too.
        Note that this bootstraps straight away and can therefore raise `BlockedError`;
        callers reacting to a refusal want `discard_blocked` instead.
        """
        if blocked:
            await self.discard_blocked(tld)
        else:
            await self.invalidate(tld)
        return await self.get(tld)

    async def bootstrap(self, tld: str) -> Session:
        """Load the site's homepage the way a browser would, and keep what it sets."""
        identity = hdr.pick_identity(chromium_only=self._impersonate)
        proxy = self._proxies.acquire()
        root = urls.site_root(tld)
        if self._pool is not None:
            # A new session has to be new all the way down. The client keeps its own
            # cookie jar and open connections, and anti-bot systems link every "fresh"
            # visitor that arrives on them back to the old, possibly flagged one. Rebuild
            # the route's client so the handshake genuinely starts from nothing.
            await self._pool.discard(proxy)
            transport: Transport | None = self._pool.get(proxy)
        else:
            transport = self._transport
        if transport is None:  # pragma: no cover - one of the two is always set
            raise RuntimeError("SessionManager was built without a transport")

        if self._budget is not None:
            await self._budget.acquire(tld)

        try:
            response = await transport.get(
                root,
                headers=hdr.document_headers(tld, identity),
                follow_redirects=True,
            )
        except TransportError as exc:
            self._proxies.bench(proxy, QUARANTINE_NETWORK_S, "could not connect")
            raise NetworkError(f"could not reach {root}: {exc}") from exc

        if response.status_code == HTTPStatus.FORBIDDEN:
            self._proxies.bench(proxy, QUARANTINE_BLOCKED_S, "refused at the homepage")
            raise BlockedError(
                f"vinted.{tld} refused the connection before we could get a session. "
                "This is usually the address you are coming from rather than the app; "
                "see docs/troubleshooting.md."
            )
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise NetworkError(f"vinted.{tld} answered {response.status_code} on the homepage")

        cookies = dict(response.cookies)
        if SESSION_COOKIE not in cookies:
            raise BlockedError(
                f"vinted.{tld} served a page but set no {SESSION_COOKIE} cookie. "
                "Run the check in docs/troubleshooting.md to see whether your address is "
                "being challenged."
            )

        session = Session(
            tld=tld,
            cookies=cookies,
            identity=identity,
            created_at=int(time.time()),
            proxy=proxy,
        )
        await self._save(session)
        self._cache[tld] = session
        log.info("session.created", tld=tld, cookies=len(cookies), via_proxy=proxy is not None)
        return session

    async def note_request(self, session: Session) -> None:
        """Record that a session was used. Cheap bookkeeping the health view reads."""
        session.request_count += 1
        await self._db.execute(
            "UPDATE sessions SET last_used_at = ?, request_count = ? WHERE tld = ?",
            (int(time.time()), session.request_count, session.tld),
        )

    async def merge_cookies(self, session: Session, new_cookies: dict[str, str]) -> None:
        """Fold cookies from a response back into the stored session."""
        if not new_cookies:
            return
        session.cookies.update(new_cookies)
        await self._save(session)

    async def _save(self, session: Session) -> None:
        await self._db.execute(
            "INSERT INTO sessions (tld, cookies_json, user_agent, created_at, last_used_at, "
            "request_count) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tld) DO UPDATE SET cookies_json = excluded.cookies_json, "
            "user_agent = excluded.user_agent, created_at = excluded.created_at, "
            "last_used_at = excluded.last_used_at, request_count = excluded.request_count",
            (
                session.tld,
                json.dumps(session.cookies),
                session.identity.user_agent,
                session.created_at,
                int(time.time()),
                session.request_count,
            ),
        )

    async def _load(self, tld: str) -> Session | None:
        row = await self._db.fetch_one(
            "SELECT cookies_json, user_agent, created_at, request_count FROM sessions "
            "WHERE tld = ?",
            (tld,),
        )
        if row is None:
            return None

        try:
            cookies = json.loads(row["cookies_json"])
        except json.JSONDecodeError:
            await self.invalidate(tld)
            return None

        if not isinstance(cookies, dict) or SESSION_COOKIE not in cookies:
            await self.invalidate(tld)
            return None

        return Session(
            tld=tld,
            cookies=cookies,
            identity=hdr.identity_for_user_agent(row["user_agent"]),
            created_at=int(row["created_at"]),
            request_count=int(row["request_count"]),
        )
