"""The parts of a web app that keep it honest when someone other than its owner finds it.

The dashboard is meant for a home network or a tailnet, but "meant for" is not a control.
So: response headers that stop the page being framed, sniffed or scripted from elsewhere;
a same-origin rule for anything that changes state, which is what stands in for CSRF
tokens when the session is a cookie; and a brake on the login form so a token cannot be
guessed at network speed. None of it needs configuration, none of it gets in the owner's way.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

# Inline styles and scripts are how this app is built (one template, no bundler), so those
# two get 'unsafe-inline'. Everything else is locked to this origin; listing photos come
# from Vinted's CDN, whose hostnames vary, hence the https: allowance for images only.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


class SecurityHeadersMiddleware:
    """Adds the headers above to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SameOriginMiddleware:
    """Refuses state-changing requests that arrive from another site.

    A browser sends `Origin` (or at least `Referer`) with every cross-site form post; when
    it names a different host than the one we are being addressed as, the request was not
    made by the owner clicking in the dashboard. Requests that carry no browser provenance
    at all — curl, n8n posting a verdict with a bearer token — are left alone; they cannot
    be a CSRF, because CSRF needs the browser's cookie.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and scope["method"] in _STATE_CHANGING:
            request = Request(scope)
            if not _same_origin(request):
                response = JSONResponse({"detail": "cross-site request refused"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _same_origin(request: Request) -> bool:
    provenance = request.headers.get("origin") or request.headers.get("referer")
    if not provenance or provenance == "null":
        # No browser provenance: not a cross-site form post. (Cookie-bearing requests from
        # a browser always carry Origin on POST; a missing one means a non-browser client.)
        return "cookie" not in request.headers or request.headers.get("sec-fetch-site") in (
            None,
            "same-origin",
            "none",
        )
    origin_host = urlsplit(provenance).netloc.lower()
    our_host = (request.headers.get("host") or "").lower()
    return origin_host == our_host


class LoginThrottle:
    """After a few wrong tokens from one address, make it wait.

    Enough to turn guessing a 64-hex token from impossible-in-theory into
    impossible-in-practice-too, without ever locking the owner out for long.
    """

    def __init__(
        self, *, attempts: int = 5, window_s: float = 600.0, cooldown_s: float = 60.0
    ) -> None:
        self._attempts = attempts
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def retry_after(self, client: str, now: float | None = None) -> float:
        """Seconds this client must wait before another try; zero when it may try now."""
        now = time.monotonic() if now is None else now
        until = self._blocked_until.get(client, 0.0)
        return max(0.0, until - now)

    def failed(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        recent = self._failures[client]
        recent.append(now)
        while recent and now - recent[0] > self._window_s:
            recent.popleft()
        if len(recent) >= self._attempts:
            self._blocked_until[client] = now + self._cooldown_s
            recent.clear()

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)
        self._blocked_until.pop(client, None)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]
