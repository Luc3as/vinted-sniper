# Security

## Reporting

Found something? Open a private security advisory on GitHub (Security → Report a
vulnerability) or write to the address on the maintainer's profile. Please do not open a
public issue for anything that could let someone else read your alerts or drive your
searches. You will get an answer within a week.

## What this is, in security terms

A single-tenant, self-hosted poller with a small web dashboard and a Telegram bot. It holds
one secret worth protecting — `WEB_AUTH_TOKEN`, which is both the dashboard password and the
bearer the agent uses — plus your Telegram bot token and whatever destination URLs you
configure. It stores public Vinted listings; nothing about you beyond your searches.

It never logs in to Vinted, never buys, never lists, never messages anyone. It reads the
public catalog anonymously.

## Threat model and what is in place

| Threat | Control |
|---|---|
| Someone on the network opens the dashboard | `WEB_AUTH_TOKEN` gates every page and every API call except `/healthz`. Set it. Without it the app logs `web.no_password` and the dashboard is open to anyone who can reach the port — fine on `127.0.0.1`, not fine on `0.0.0.0`. |
| Guessing the token | Constant-time comparison; five wrong tries from one address earn a 60 s cooldown (`429`, `Retry-After`). Use `openssl rand -hex 32`. |
| Cross-site request forgery against the dashboard's forms | Session cookie is `HttpOnly`, `SameSite=Strict`, `Secure` behind HTTPS; state-changing requests whose `Origin`/`Referer` name another host are refused (403); CSP `form-action 'self'`. |
| Cross-site scripting | Jinja autoescape everywhere; no inline event handlers (confirmations live in data attributes); CSP restricts scripts and styles to this origin and inline. Text from Vinted (titles, seller names) and from the agent (model, verdict) is escaped in HTML, Telegram HTML and RSS alike. |
| Clickjacking, MIME sniffing, referrer leaks | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. |
| Token in logs or on the History page | Telegram error messages have the bot token redacted before they are stored; `httpx`/`httpcore` loggers run at WARNING; the dashboard token is never logged. |
| SQL injection | Parameterised queries throughout; the only string-built SQL is a fixed whitelist of column names and `?` placeholder lists. |
| A malicious Telegram user pressing buttons | Callback buttons are honoured only from a chat that is a paired destination. |
| The agent (webhook) being someone else | Verdicts need the bearer token. The payload the agent receives never contains it. |
| Supply chain | `uv.lock` pins everything; `pip-audit` reports no known vulnerabilities as of this release; CI runs `gitleaks`. |
| Container | Runs as an unprivileged user, read-only root filesystem, `no-new-privileges`, only `/data` writable. |

## What is deliberately *not* in place

- **No multi-user model.** One token, one owner. Anyone with the token can do everything,
  including exporting destination URLs and chat ids from `/api/export`. Treat the token like a
  password to all of it.
- **No TLS.** Put a reverse proxy (Caddy, nginx, Tailscale Serve) in front if the dashboard is
  reachable beyond your LAN. The cookie's `Secure` flag turns on automatically behind HTTPS.
- **No outbound URL allow-list.** Webhook destinations are URLs you type; the app will POST to
  them, including addresses on your own network. That is the point of a webhook, but it means
  the token holder can make the app talk to anything the container can reach.
- **No rate limit on the API.** The token holder is you.

## Publishing the code

The code contains no secrets (`detect-secrets`, `gitleaks`), no vendored credentials, no test
fixtures with real tokens. `.env` is gitignored; `.env.example` has placeholders only. The
name is a nominative reference to Vinted; the project is not affiliated with Vinted and the
README says so. Automated access to Vinted is against its terms of service — see
`docs/legal.md`; running the tool is the operator's decision, publishing the code is not the
same act.
