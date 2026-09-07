# Changelog

All notable changes, newest first. Dates are when the change landed on `main`.

## Unreleased

### Fixed
- A session Vinted had flagged was reborn with the same cookies: curl_cffi keeps its cookie
  jar on the pooled transport, so every "fresh" session after a refusal carried the same
  DataDome cookies and stayed blocked until the container restarted. A new session now gets
  a new transport — new cookie jar, new connections. And the identity no longer flips
  browsers mid-jar: with `HTTP_IMPERSONATE` on, personas are Chromium-only, matching the
  TLS fingerprint actually presented.
- Alert times were UTC ("Listed 20:15 UTC"); they now read in `TIMEZONE` ("Listed at
  22:15"), which is where the reader lives.
- The watchdog called niche searches "stale" whenever a busy search on the same site kept
  finding listings — "Rab Downpour" measured against "waterproof jacket" looked frozen forever.
  A search whose page is not even full is never stale, and a full-page search is judged
  against its own usual gap between listings (six times it, two hours at least).
- Console logs no longer use ANSI colours unless `LOG_COLOR=true`; Portainer's log viewer
  rendered them as a smear of repeated words.
- The "Message seller" and "Buy" buttons under Telegram alerts linked to Vinted deep links
  (`/items/{id}/want_it/new`, `/transaction/buy/new`) that have answered "page not found" since
  the site's front end was rebuilt. They are replaced by "Seller profile"; asking and buying
  happen from the listing page. The webhook's `links` keep their keys (now resolving) and
  gain `seller`.
- A refusal during the session handshake (the anti-bot challenge page) crashed the search's
  task; the supervisor restarted it every fifteen seconds and every search on the site
  loaded the homepage four times a minute until the address was blocked outright. A refusal
  now drops the session without fetching another and backs off as intended.
- Searches starting on an empty cache each performed their own handshake — seven homepage
  loads with seven browser personas inside one second, from one address. One bootstrap now
  runs per site at a time and everyone shares the result.
- The watchdog could crash, or rotate a session into a site that was already holding us off.

### Added
- **Found page**: `/` now shows what turned up — listing photo cards with full galleries,
  the total price leading (the asking price as a muted "+ fee" footnote), a text filter,
  drops-only, and Show more. Searches, destinations and export moved to `/searches`.
  Tooltips rebuilt to survive scrolling tables, a theme toggle (system → light → dark),
  destinations editable and re-enablable in place, and a Playwright UI suite (`tests/ui`)
  checking tooltip geometry, dark mode and responsive layouts for real.
- **Site-wide cooldown and request budget.** One refused search holds every search on that
  site (`poll.cooling_down`, shown as "cooling" in the dashboard and `/status`, kept across
  restarts). `SITE_REQUESTS_PER_MINUTE` caps the address as a whole, homepage loads included.
  `STARTUP_STAGGER_S` spreads first checks out. The first refusal on a site is announced to
  status destinations.
- **Title and seller filters**: `--require`, `--title-regex`, `--min-seller-rating`,
  `--min-seller-reviews`, `--block-seller`; also under "More filters" in the dashboard.
- **Buttons under each Telegram alert**: skip this seller for the search, pause the search.
  `/resume <id>` undoes the pause. Buttons only work from a paired chat.
- **Quiet hours per destination** with a digest when they end; `TIMEZONE` setting. A
  dashboard toggle for health notices per destination.
- **Price drops**: a listing already announced is announced again when its total price
  falls by `PRICE_DROP_MIN_PERCENT`, at no extra requests.
- **Dashboard**: edit a search (everything but the URL), live refresh from `/api/health`,
  next-check countdown, bulk interval change, price-drop and verdict badges on listings.
- **Enrichment loop**: hand each listing to an outside agent via the webhook destination's
  `enrichment_url`; a verdict posted to `/api/items/{id}/enrichment` (deal score, identified
  model, retail price, match, risk, one line) is woven into the alert — hot deals headlined,
  dull ones muted. Late hot verdicts get a short follow-up. See `docs/enrichment.md`.
- **Languages for alerts**: each destination has a language (`--lang en|sk`, selector in the dashboard); alerts, bot replies, health notices and the weekly report are rendered in it, with Slovak plural forms. A JSON catalog per language in `locales/`, no gettext toolchain. The dashboard stays English for now.
- **Clone a search to another country** from its Edit form: same filters and destinations on vinted.de/.pl/.cz/…; a listing shown on several sites is alerted once.
- **"Only the cheapest N%" filter** (`--cheapest`, or the field in the dashboard): the listing's price against everything the search has seen in 30 days. Alerts say the position in words ("cheaper than 88% of 312 similar listings seen this month").
- **Dashboard rewritten for people who have not read the docs**: plain-language `?` tooltips on every field and column, a state legend, "how the filters work together" explainers, a Help page with a glossary and the running settings, success notices after every action, confirmations on delete, SVG icons, a sticky nav with the current page marked, keyboard focus rings, reduced-motion support, and dark mode that no longer has hard-coded light colours.
- **Delivery history** page (`/history`, `/api/history`): every notification, its kind, where it went, sent/pending/failed with the error, held-until for enrichment — the answer to "did my alert go out?".
- Telegram `/pause <id>` alongside `/resume <id>`.
- **Market memory**: every listing on a search's page is recorded as a price point (no extra requests). The webhook payload gains `market` (percentiles, the listing's own percentile, median for the same condition, the price under which listings vanish within a day), demand (`favourites_per_hour`), `known_retail` (retail prices earlier verdicts found, cached per search) and `buyer_feedback` (👍/👎 the buyer gave earlier verdicts, via new buttons under Telegram alerts).
- **Weekly report** to status destinations on Monday morning (`WEEKLY_REPORT`).
- **`export` / `import`** of searches, destinations and routes as one JSON document; also
  `/api/export` and `/api/import`.
- The Docker image now includes the `impersonate` extra.

### Security
- Security headers on every response (CSP with `frame-ancestors 'none'` and `form-action 'self'`, `X-Frame-Options`, `nosniff`, `Referrer-Policy`); state-changing requests from another origin are refused; session cookie is `SameSite=Strict` and `Secure` behind HTTPS; five wrong tokens from one address earn a 60 s cooldown; no inline event handlers in templates; the Telegram bot token is redacted from stored error messages. `SECURITY.md` documents the threat model.

### Changed
- New identity: a coat hanger whose hook is a target ring (mark), the same inside a scope (icon/favicon), and a wordmark set in Outfit converted to paths. Logo files carry their own licence (all rights reserved; code stays MIT).
- New README (English with a Slovak section) and an original logo (`docs/media/logo/`, light and
  dark, mark and wordmark); the mark is inlined in the dashboard header and favicon. Upstream's
  README is kept as `README.upstream.md`.
- `filters.check()` is a pipeline of small gates.
- The webhook payload gained additive fields at contract version 1: `event`,
  `previous_total_price`, `photo_urls`, `seller_reviews`, `enrichment_url`, `search_id`.
- Rate-limit primitives moved to `vinted_sniper.ratelimit`; `deliver.ratelimit` re-exports.

### Database
Migrations 0004–0012: search filter columns, destination quiet hours, outbox rebuilt twice
(notification kinds, previous price), item enrichment columns, market memory and buyer
feedback, market percentiles on notifications, destination language. Applied automatically
at startup; all additive, existing rows behave as before.
