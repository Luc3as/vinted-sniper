# vinted-sniper — security audit before publishing (2026-09-06)

Scope: the whole fork at `0ee3b90` (upstream `9c462a9` + 22 patches). Method: automated
scanners plus a manual read of every request handler, sender and the Telegram bot against the
OWASP Top 10 and the `engineering:code-review` checklist. No dedicated "security scan" skill
exists in the catalog; the tooling below is the industry-standard substitute and is what CI
should run before each release.

## Tooling and results

| Tool | What it checks | Result |
|---|---|---|
| `bandit -r src` | Known-dangerous Python patterns | 7 findings, all reviewed, **none exploitable** (3× string-built SQL that only interpolates a whitelisted column name or a `?,?,?` placeholder list; 2× `random` used for polling jitter, not security; `0.0.0.0` bind default inside the container; `xml.sax.saxutils.escape` used for *output*, not parsing) |
| `ruff --select S` | Same class, different rule set | Same 6 items, same verdict |
| `pip-audit` on the locked runtime deps | Known CVEs | **0 vulnerabilities** (httpx, aiogram, fastapi, uvicorn, pydantic, aiosqlite, structlog, curl_cffi…) |
| `detect-secrets --all-files` | Credentials in the tree | Only cache markers and a library docstring in `.venv`; **nothing in the repo** |
| `git ls-files` grep | `.env`, keys, tokens, personal ids | None tracked; `.env`, `data/`, `*.db*` are gitignored. The owner's name appears only in the logo licence (intended) and GitHub URLs |
| Manual: HTML parser over every page | Unbalanced markup | Clean |

## Findings fixed in patch 0022

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | **CSRF**: dashboard forms authenticated by a `SameSite=Lax` cookie with no origin check; any site the owner visited could POST `/searches/…/delete`, change destinations, or import config | 🟠 High (needs the owner to be logged in and lured) | Cookie `SameSite=Strict` + `Secure` behind HTTPS; middleware refuses state-changing requests whose `Origin`/`Referer` host differs; CSP `form-action 'self'` |
| 2 | **JS-in-attribute XSS**: `onsubmit="return confirm('Delete “{{ search.name }}”…')"` — Jinja autoescape protects HTML, not a JavaScript string inside an attribute; a search name containing `'` became code. Attacker = whoever names a search (the owner, or a crafted import file) | 🟡 Medium (self-inflicted in single-tenant use, real if the token leaks) | Confirmations moved to `data-confirm` + one listener; test asserts no inline handlers exist |
| 3 | **No security headers**: page could be framed (clickjacking of forms), scripts could be injected from anywhere if any XSS existed | 🟡 Medium | CSP (`default-src 'self'`, `frame-ancestors 'none'`, `img-src https:` for Vinted photos), `X-Frame-Options DENY`, `nosniff`, `Referrer-Policy no-referrer`, `Permissions-Policy` |
| 4 | **Login brute force**: constant-time compare but unlimited attempts | 🟡 Medium (token is 64 hex → infeasible, but nothing stopped a try) | 5 wrong tokens per address → 60 s cooldown, `429` + `Retry-After`, `web.login_failed` logged |
| 5 | **Bot token in error strings**: an `httpx` transport error can include the request URL, which contains `bot<TOKEN>`; it would have been stored in `outbox.last_error` and shown on `/history` | 🟢 Low | Redacted before storage |

## Reviewed and acceptable as-is (documented in `SECURITY.md`)

- **Auth model**: one token = full control, including `/api/export` of destination URLs and chat ids. Correct for a single-tenant tool; the README and Help page now say to treat it as a password.
- **Session cookie = the token itself.** `HttpOnly` so scripts cannot read it; leaking it is equivalent to leaking the token. Acceptable; noted.
- **SSRF by design**: webhook destinations are owner-typed URLs the app POSTs to, including RFC1918 addresses. Necessary for n8n/Home Assistant on the same network. Only the token holder can add one.
- **Enrichment endpoint**: bearer-protected, body validated by Pydantic with length caps, only updates a row that exists; verdict text is escaped in every renderer. The agent never receives the token.
- **Telegram callbacks**: honoured only from a chat that is a paired destination; callback data parsed defensively.
- **RSS**: token in query string (feed readers cannot log in) — documented; XML-escaped output.
- **Vinted data**: titles, seller names, photo URLs are untrusted input and are escaped in HTML (Jinja), Telegram (`html.escape`), RSS (`xml_escape`), Discord (plain text fields). Photo URLs are only ever passed to clients, never fetched server-side by the sniper (n8n fetches them; that is n8n's sandbox).
- **SQL**: `aiosqlite` with parameters everywhere; the three f-string sites interpolate constants.
- **Container**: non-root uid 10001, read-only rootfs, `tmpfs /tmp`, `no-new-privileges`, single writable bind mount. Health check runs the app's own heartbeat.
- **Logs**: `httpx`/`httpcore` at WARNING (no URLs with tokens); dashboard token never logged; `KEEP_RAW_JSON` off by default so seller payloads are not retained.

## Residual risks the operator owns

1. **Exposure of port 8000.** The compose in the fork's README binds `8000:8000` (LAN). Put Caddy/nginx/Tailscale Serve with TLS in front if it must be reachable beyond the LAN; the cookie's `Secure` flag follows automatically.
2. **Terms of service.** Automated access to Vinted violates its ToS; `docs/legal.md` covers it. Publishing the code is lawful and common (see upstream and every notifier on GitHub); running it is the operator's decision. The name is a nominative use of "Vinted"; the README states non-affiliation.
3. **Dependency drift.** `pip-audit` is clean today; add it to CI so it stays that way (a one-line job next to `gitleaks`).
4. **`WEB_AUTH_TOKEN` unset.** The app runs open on purpose for `127.0.0.1` setups and warns in the log. On any other bind, set it.

## Verdict

**Safe to publish.** No secrets, no personal data, no vulnerable dependencies, no injection paths; the four web-hardening gaps a LAN tool typically ships with are closed in 0022 and covered by tests (482 passing, 8 skipped). Before tagging a release: run the four scanners above (they take under a minute), and consider adding `pip-audit` to `ci.yml`.
