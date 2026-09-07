# vinted-sniper — handoff for coudy (Claude Code + GSD-PI)

Written 2026-09-06 by Claude (claude.ai session) for the next agent. Everything here was
verified in the session unless marked *unverified*. Read this before mapping the codebase;
it tells you what the code cannot: why things are the way they are and what is still guesswork.

---

## 0. TL;DR

- Upstream: `jasp-nerd/vinted-sniper` (Python 3.13, uv, MIT). Fork: `Luc3as/vinted-sniper`.
- 25 commits on top of upstream `9c462a9`, shipped as `0001…0025-*.patch` next to this file.
  Apply with `git am`, in order. **484 tests collected, all green** (8 skipped by design in `test_i18n.py`),
  ruff / mypy / deptry / vulture clean.
- Runtime: Docker via Portainer on **coudy** (moving there from NUC2 on 2026-09-06). Repo at
  `/opt/AI_projects/vinted-sniper`, runtime data at `/opt/AI_projects/vinted-sniper-data`
  (bind-mounted to `/data`), local image `vinted-sniper:impersonate`. The path is `AI_projects`
  (lower-case "projects") — Linux is case-sensitive and a wrong path gives Portainer an empty
  bind mount and a fresh, unpaired database.
- Brain: n8n workflow **"Vinted Sniper · AI verdikt inzerátu"** (`onMGFuooAsSZATob`,
  folder *Vinted Sniper*, n8n.lukasporubcan.sk) — Claude Sonnet 4.5 via OpenRouter + SerpApi.
- **Stop adding features.** The next round is calibration from production data (§7).

---

## 1. Bootstrap on coudy

```bash
mkdir -p /opt/AI_projects/vinted-sniper && cd /opt/AI_projects/vinted-sniper
git clone https://github.com/jasp-nerd/vinted-sniper.git .        # upstream
git remote add fork git@github.com:Luc3as/vinted-sniper.git
git checkout -b luc3as/main 9c462a9                                 # the base the patches expect
git am /path/to/handoff/000*.patch                                  # all 17, in order
uv python install 3.13 && uv sync --extra web --extra impersonate --group dev
uv run pytest -q                      # expect 482 passed, 8 skipped
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src && uv run deptry . && uv run vulture src --min-confidence 80
git push -u fork luc3as/main
```

If `git am` conflicts: the patches were generated from a linear history; check whether upstream
`main` moved past `9c462a9` (it had not as of 2026-09-06). Do **not** rebase onto a newer
upstream blindly — see §8 for what we depend on.

GSD-PI: initialise the project here; let it map the codebase, but feed it this file first —
the "why" is not in the code.

---

## 2. What the system is (after the patches)

```
Vinted catalog API ──(session cookie, browser TLS via curl_cffi)──► Poller (one per search)
                                                                        │
       market table ◄─ every listing on the page (price points) ◄────────┤
       items table  ◄─ listings that passed filters ─────────────────────┤
       outbox       ◄─ notifications (kind: new | price_drop | verdict) ◄┘
                          │
                     Dispatcher (one worker per destination, rate-limited)
                          ├─► Telegram / Discord / ntfy
                          └─► webhook (n8n)  ──► LLM agent ──► POST /api/items/{id}/enrichment
                                                                     └─► releases held alert, or
                                                                         queues a "verdict" follow-up
```

Key modules (all `src/vinted_sniper/`):

| Area | Files | Notes |
|---|---|---|
| Pacing / anti-block | `vinted/pacing.py`, `vinted/session.py`, `engine/poller.py` | per-TLD request budget, site-wide cooldown, one bootstrap per TLD (lock), staggered start. **This is what fixed the production crash** (§3.1). |
| Filters | `engine/filters.py` (gate pipeline), `engine/poller.py::_market_positions` | market-percentile gate lives in the poller because it needs the DB. |
| Delivery | `deliver/dispatcher.py`, `deliver/{telegram,discord,ntfy,webhook}.py` | quiet hours, enrichment hold, verdict rendering. Webhook payload is a versioned contract (v1, additive fields only). |
| Enrichment loop | `enrichment.py`, `web/server.py::post_enrichment`, `db/repo.py::store_enrichment` | contract in `docs/enrichment.md`. |
| Market memory | `db/repo.py` (`observe_market`, `market_context`, `market_prices`, `record_market_position`, `known_retail`, `feedback_examples`) | migration 0010/0011. |
| Bot | `botctl/telegram_bot.py` | callbacks `bs:`, `ps:`, `fb:`; `/pause`, `/resume`, `/status`. Only paired chats may press buttons. Replies in the paired destination's language. |
| i18n | `i18n.py`, `locales/*.json` | `Translator.gettext/ngettext`, `{name}` placeholders, plural forms per language. Add a language: new JSON + plural rule. `tests/unit/test_i18n.py` checks placeholders and that every translated string still exists in the code. |
| Web | `web/server.py`, `web/templates/{base,dashboard,history,help,login}.html` | server-rendered Jinja, no build step, single inline stylesheet with light/dark tokens. |
| Reports | `engine/report.py` (Monday 08:00), `engine/watchdog.py` | |
| Backup | `backup.py`, CLI `export`/`import`, `/api/export`, `/api/import` | |

Migrations `db/migrations/0004…0012` are all additive; two of them (0006, 0008) rebuild `outbox`
because SQLite cannot widen a CHECK constraint. Applied automatically at startup.

---

## 3. History of the 17 patches (why each exists)

| # | Commit | Why |
|---|---|---|
| 0001 | Survive a refusal instead of amplifying it | Production: DataDome challenge → `BlockedError` re-raised inside the handler → poller task died → supervisor restarted every 15 s → 7 searches × homepage every 15 s until the IP was blocked. Plus a thundering herd: 7 bootstraps with 7 random User-Agents in one second. Fixes: `discard_blocked()`, per-TLD lock, `SiteCooldown`, `RequestBudget`, stagger, block alert to status destinations. |
| 0002 | Title & seller filters | Vinted text search matches descriptions. |
| 0003 | Telegram inline actions | Skip seller / Pause search from the alert. |
| 0004 | Quiet hours + digest | + `TIMEZONE`, + health-notice toggle in UI (0001's alert had nowhere to go otherwise). |
| 0005 | Price drops | Compares known items' prices against the page just fetched: zero extra requests. |
| 0006 | Dashboard edit/live/bulk | `Query.updated_at` in the supervisor signature so edits reach the poller without restart. |
| 0007 | "cooling" state persisted | Restart used to forget the hold and fire a fresh volley. |
| 0008 | Enrichment loop | Hold chat alerts `ENRICHMENT_WAIT_S`, webhook fires at once, verdict releases. Silence from the agent costs a delay, never an alert. |
| 0009 | Late-verdict follow-up | Only for hot verdicts (≥ `ENRICHMENT_HIGHLIGHT_SCORE`). |
| 0010 | Weekly report | Monday 08:00 in `TIMEZONE`, slot stored so restarts don't repeat. |
| 0011 | Export / import | Additive merge by search URL and destination name. |
| 0012 | CHANGELOG | Use as PR description material. |
| 0013 | `/history` page + `/pause` | Answers "items found but no Telegram" without docker logs. |
| 0014 | Dead deep links | `/items/{id}/want_it/new` and `/transaction/buy/new` 404 since Vinted rebuilt its front end (every notifier on GitHub still copies them). Buttons now: Open listing · Seller profile (`/member/{id}`). **Seller profile link is unverified in production** — if it 404s, try `/member/{id}-{login}`. |
| 0015 | Watchdog by own pace + log colours | Niche searches (page not full) were called "stale" against busy neighbours; fixed. ANSI colours smeared Portainer's log viewer ("INF INF INF…"); off unless `LOG_COLOR=true`. |
| 0016 | Market memory, retail cache, 👍/👎 | The agent gets `market`, `known_retail`, `buyer_feedback`, demand (`favourites_per_hour`). |
| 0017 | Clone to another country, "cheapest N%" filter, dashboard UX pass | Tooltips on everything, legend, Help page, success notices, confirmations, SVG icons, dark-mode tokens. **Never rendered in a real browser** — see §5. |
| 0022 | Web hardening before publishing | `web/security.py`: security headers, same-origin check on POST, login throttle; SameSite=Strict cookie; no inline JS handlers; bot token redaction. `SECURITY.md` = threat model. Audit tooling: `bandit`, `ruff --select S`, `pip-audit`, `detect-secrets` — all clean; re-run before each release. |
| 0024 | SECURITY.md audit section | Commands to re-run bandit / pip-audit / ruff S; the three bandit findings that are false positives, with reasons. |
| 0023 | Tagline "See it first. Know if it's worth it." | Wordmark regenerated; short second line "Faster than the favourites." for footers. |
| 0021 | New identity: hanger-with-target mark, scope icon, Outfit wordmark as paths | `docs/media/logo/` (own licence, not MIT); header mark and favicon in `base.html`. Generated by `fontTools` + `cairosvg` from a script that is not in the repo — regenerate from the SVGs if needed. |
| 0020 | `reader_language` in the webhook payload | The agent learns which language the buyer reads this search's alerts in and writes the verdict in it. |
| 0019 | Logo + README rewrite (`04ada18`) | Original SVG mark (price tag in a reticle — superseded by 0021's hanger), README rewritten for the fork with a Slovak section; upstream README kept as `README.upstream.md`. |
| 0018 | Per-destination language (en/sk) for alerts, bot, notices, weekly report | `i18n.py` + `locales/sk.json`; gettext-shaped, no toolchain. **UI is still English** — phase 2 (templates) is the natural next i18n step; Jinja's i18n extension can take `Translator.gettext`/`ngettext` directly. Also fixed: 0017's 📊 market line was never passed to the Telegram payload. |
| 0019 | Logo + fork README | Original SVG mark/wordmark in `docs/media/logo/` (light/dark), inlined in header + favicon; README in the owner's bilingual style; upstream README kept as `README.upstream.md`. |

---

## 4. Production deployment (coudy; NUC2 was the original host)

Migration NUC2 → coudy: `docker stop vinted-sniper` on NUC2 first (SQLite WAL; and two live
instances would double the request rate on one public IP and fight over the Telegram bot's
`getUpdates`), `scp /opt/vinted_sniper/data/app.db*` to coudy, place in
`/opt/AI_projects/vinted-sniper-data/`, `chown -R 10001:10001`. Copying the DB (not
export/import) keeps the Telegram pairing, market memory, outbox and sessions. Migrations
0004–0011 apply on first start. Keep the NUC2 data directory as a fallback for a week.

- Stack (Portainer on coudy, no re-pull — image is local): image `vinted-sniper:impersonate`,
  port `8000:8000`, bind `/opt/AI_projects/vinted-sniper-data:/data` (uid 10001),
  `read_only: true`, `tmpfs /tmp`. Build from the repo: `docker build -t vinted-sniper:impersonate .`
- Env that must be set: `VINTED_SNIPER_TELEGRAM_BOT_TOKEN`, `VINTED_SNIPER_WEB_AUTH_TOKEN`,
  `VINTED_SNIPER_HTTP_IMPERSONATE=true`, `VINTED_SNIPER_TIMEZONE=Europe/Bratislava`,
  `VINTED_SNIPER_ENRICHMENT_WAIT_S=90`, `VINTED_SNIPER_WEB_PUBLIC_URL=http://<coudy-LAN-IP>:8000`
  (the webhook's `enrichment_url` is built from this and n8n — also on coudy — must be able to
  reach it; `localhost` inside the n8n container breaks the loop. If both containers share a
  Docker network, `http://vinted-sniper:8000` works too).
- Telegram bot `@luc3as_vinted_bot`, chat already paired. Health notices must be ON for that
  destination or block alerts and the Monday report go nowhere.
- Rebuild after every change: `docker build -t vinted-sniper:impersonate .` then Update the stack.
  Dockerfile already includes `--extra impersonate`.
- Upstream `docker.yml` publishes `ghcr.io/${{ github.repository }}` on push to `main` → merging
  `luc3as/main` into the fork's `main` with Actions enabled yields `ghcr.io/luc3as/vinted-sniper`.
- Vinted from the home Orange IP: plain `curl -c - https://www.vinted.sk/ | grep access_token_web`
  returns a cookie (verified 2026-09-06 15:00). Blocks seen so far were self-inflicted (0001).

Current production searches (7, all `.sk`, ~10 min interval): Nord Blanc muži, Zelené
windstopperky, Nepremokavá bunda, GymGlamour BBL, Patagonia Torrentshell, Rab Downpour, MH500.
`WATCHDOG_ACTION=warn` at the moment; can go back to `rotate` after 0015 proves itself.

---

## 5. Verify the UI with the headless browser (first task)

Patch 0017 rewrote every template and was checked only by an HTML parser and TestClient. Do this
before anything else:

1. Run locally: `VINTED_SNIPER_DB_PATH=/tmp/vs.db uv run vinted-sniper run` (web on :8000, no token
   needed), seed a search + destination through the UI or `vinted-sniper watch`.
2. Screenshot `/`, `/history`, `/help`, `/login` at 375 px, 768 px, 1280 px, in both
   `prefers-color-scheme` modes. Check: tooltips (`.help` hover/focus) not clipped at viewport
   edges; tables scroll inside `.table-wrap` rather than the page; the Edit row and the Clone form
   inside it; the search builder (`#builder`, unchanged JS) still opens and populates categories
   (needs live Vinted; expect a 502 offline); the `?ok=` / `?error=` flash renders once.
3. Contrast: muted text is `--muted` on `--panel`; verify ≥ 4.5:1 in both modes.
4. Known nit: the tooltip for "Title matches pattern" avoids backslashes because Jinja string
   literals don't unescape `\\b` the way I expected; if you want a regex example with `\b`, pass
   it from the view context instead of a template literal.

Keep the no-build-step, single-file-CSS approach; the upstream author's style is inline Jinja.

---

## 6. n8n side

Workflow `onMGFuooAsSZATob` (folder *Vinted Sniper*). Nodes: Webhook `POST /webhook/vinted-sniper-enrich`
→ Code (unwrap `body.items`, keep `event == "new"`) → Code (download ≤3 photos as binary)
→ AI Agent (OpenRouter `anthropic/claude-sonnet-4.5`, tools: SerpApi Google Search, HTTP tool
"Google Lens" via `serpapi.com/search.json?engine=google_lens`, structured output parser)
→ HTTP Request POST to `enrichment_url` with Bearer = `WEB_AUTH_TOKEN`.

The system prompt scores primarily on `market.this_percentile` / `sells_fast_under`, then demand,
then retail (secondary); authenticity risk caps at 35, `matches_query=false` caps at 20; verdict is
one Slovak sentence. Credentials still to be filled by Lukáš: SerpApi, Query Auth `api_key`
(Lens), Bearer. OpenRouter is assigned. Cost ≈ 2–3 ¢ per listing. SerpApi free tier: 250/month —
retail cache (`known_retail`) exists to keep that in budget; Lens is a fallback the prompt tells the
agent to use only when photos + title fail.

Do not move delivery into n8n: buttons, quiet hours, digests and rate limits live in the sniper.

---

## 7. What to do next (in order)

1. **UI verification** (§5).
2. **Calibration after ≥ 1 week of production.** Pull from NUC2:
   `docker logs vinted-sniper | grep -E "poll.blocked|cooling_down|catalog.paced|price_drops|enrichment.received|watchdog"`.
   Questions to answer: does 12 req/min per TLD ever pace (`catalog.paced`)? any `poll.blocked` —
   how often, is the cooldown (2×interval → backoff) long enough? does the new watchdog still
   flag niche searches? how many verdicts, score distribution, share of 🔥 vs 💤, 👍/👎 ratio?
   Then tune `SITE_REQUESTS_PER_MINUTE`, `STARTUP_STAGGER_S`, `ENRICHMENT_WAIT_S`,
   `ENRICHMENT_HIGHLIGHT_SCORE/SILENT_BELOW`, and the prompt thresholds.
3. **Before any public push of `main`**: re-run the audit in `SECURITY.md` (bandit, pip-audit, ruff S), `gitleaks` via CI, and confirm `.env` is not tracked.
4. **Upstream PRs** (as three stacked PRs; CHANGELOG.md has the wording): (a) 0001 + 0007 + 0015
   bugfixes, (b) 0014, (c) the rest as "features". Contract note for the maintainer: webhook v1
   gained only additive fields.
5. **UI translation (i18n phase 2)** when the dashboard has stopped changing: register
   `i18n.get(lang).gettext/ngettext` with Jinja's `i18n` extension, wrap template strings in
   `{% trans %}` / `_()`, add a language switch (cookie) in the header, extend `locales/sk.json`.
   ~300 strings, mostly tooltips and the Help page; have a human read the Slovak.
6. Then, and only with data: a Market page per search (price distribution, trend), a timeline of
   refusals/cooldowns, Home Assistant automation off the webhook (hot deal → TTS / phone push with
   photo), prompt few-shot from 👍/👎 once there are ≥ 20 ratings.

---

## 8. Things not to break

- `deliver/webhook.py` payload = **contract v1**. Add fields, never rename or remove.
- `test_docs_match_code.py` fails if a `Settings` field is missing from `docs/configuration.md`
  or `.env.example`, or a documented CLI command does not exist. Update all three together.
- Outbox `UNIQUE (item_id, destination_id, kind, previous_price)` with `previous_price` default 0
  — that is what makes "new once, price-drop per price, verdict once" work. Don't NULL it.
- `Poller.tick` returns per error type; `PLR0911` is intentionally silenced for it. On
  `BlockedError`: **never bootstrap inside the handler** (that was the production crash).
- `SessionManager.get()` is locked per TLD; `_get_locked` is the unlocked body. Keep it that way.
- Telegram callback data ≤ 64 bytes; `inline_actions()` drops the seller button if the login is
  too long.
- Ruff config in `pyproject.toml` has per-file ignores with a comment each; follow the pattern
  rather than adding `noqa` everywhere. Commit messages in the repo are prose paragraphs
  explaining *why*; the upstream author cares, keep the register.

---

## 9. Unverified / known unknowns

- `/member/{id}` seller link resolving on today's Vinted (0014).
- Whether Vinted's catalog API keeps returning `favourite_count` / `view_count` — the model
  tolerates their absence (defaults 0), the demand signal would just go flat.
- `sells_fast_under` heuristic: a listing gone from the first page within a day is *probably* sold;
  could also be withdrawn or pushed out on a very busy search. Treat as directional.
- The market filter and market line need ≥ 10 price points; niche searches may take days.
- Percentile jitter: `market_prices()` sorts all points per poll — fine at hundreds, revisit if a
  search accumulates tens of thousands (retention prunes at `max(ITEM_RETENTION_DAYS, 60)` days).
- OpenRouter model id `anthropic/claude-sonnet-4.5` was not verified against OpenRouter's current
  list from this session (no network to openrouter.ai); if the node errors, pick the current
  Sonnet id in the model field.

---

## 10. Files next to this document

- `0001…0025-*.patch` — the series, `git format-patch` from upstream `9c462a9`.
- `apply.sh` — helper written for the NUC2 layout (`/opt/vinted_sniper`); on coudy follow §1
  instead (paths differ).
