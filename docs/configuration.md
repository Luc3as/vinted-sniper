# Configuration

Two kinds of settings, kept apart deliberately.

**Environment variables** control the process: where the database is, how often to check,
how to log. They are read once at startup and are listed below.

**Searches and destinations** live in the database and are managed with the CLI or the
dashboard. There is no config file listing them, so there is nothing to keep in sync and no
question about which copy wins.

## Environment variables

Every variable is prefixed `VINTED_SNIPER_`. All of them are optional except where noted.
[`.env.example`](../.env.example) has the same list with comments, ready to copy.

### Storage

| Variable | Default | What it does |
|---|---|---|
| `DB_PATH` | `./data/app.db` | Where the SQLite file lives. Already set to `/data/app.db` in the container. |
| `ITEM_RETENTION_DAYS` | `30` | Delete stored listings older than this. Does not cause anything to be re-sent. |
| `KEEP_RAW_JSON` | `false` | Keep each listing's full API payload. Handy when debugging a parsing problem; stores more seller data than notifications need. |

### Checking

| Variable | Default | What it does |
|---|---|---|
| `POLL_DEFAULT_INTERVAL_S` | `60` | Seconds between checks for a newly added search. Per-search values override it. Anything under 10 is refused. |
| `FRESHNESS_WINDOW_MIN` | `20` | Ignore listings whose photo is older than this. Stops a restart from replaying old results. |
| `FIRST_RUN_MODE` | `silent` | What a brand-new search does first time: `silent` notifies nothing, `newest` sends exactly one listing so you can confirm delivery works. |
| `PRICE_DROP_MIN_PERCENT` | `10` | Announce a listing again when its total price has fallen by at least this much since it was recorded. Free: only listings still on the search's first page are compared, using the page already fetched. `0` turns it off. |
| `REQUEST_TIMEOUT_S` | `15` | How long to wait for Vinted before giving up on one request. |

The 10-second floor is not caution for its own sake: Vinted's own API lags minutes behind
what people upload, sometimes longer, so checking faster finds nothing sooner and does get
you blocked. Each notification shows both when Vinted says the listing appeared and when it
was found, so you can see the real delay yourself.

### Staying unblocked

| Variable | Default | What it does |
|---|---|---|
| `SITE_REQUESTS_PER_MINUTE` | `12` | Ceiling on requests to any one country site from this address, counted across every search and including homepage loads. When one search is refused, every search on that site waits out the same backoff — the address is what gets scored, not the search. |
| `STARTUP_STAGGER_S` | `20` | Seconds between the first checks of successive searches at startup, so a restart with ten searches does not open with ten requests in one second. |
| `SESSION_ROTATE_MINUTES` | `60` | Start a fresh anonymous session after this long. Blocks track session age more than request rate. |
| `HTTP_IMPERSONATE` | `false` | Make requests present a real browser's TLS fingerprint. Needs the `impersonate` extra. Only worth turning on if you are being blocked while the same search loads fine in a browser. |
| `PROXY_FILE` | unset | Path to a text file of proxy URLs, one per line (blank lines and `#` comments ignored). Used in turn; one that gets refused sits out for ten minutes. If all of them are sitting out, requests go direct rather than not at all. Rarely needed. |

### Noticing problems

| Variable | Default | What it does |
|---|---|---|
| `WATCHDOG_STALE_CYCLES` | `10` | Checks with no new listing before a search is treated as stuck — but only if other searches on the same site are still finding things. |
| `WATCHDOG_ACTION` | `rotate` | `warn` logs it; `rotate` also starts a fresh session. |
| `ENRICHMENT_WAIT_S` | `0` | Hold chat notifications this many seconds so an outside agent can post a verdict to `/api/items/{id}/enrichment` first. Webhook destinations fire at once regardless. `0` turns the loop off. See [enrichment.md](enrichment.md). |
| `ENRICHMENT_HIGHLIGHT_SCORE` | `75` | Deal scores at or above this are headlined as a hot deal. |
| `ENRICHMENT_SILENT_BELOW` | `40` | Deal scores below this, or a verdict that the listing is not what was searched for, are delivered without a notification sound (Telegram). |
| `OUTBOX_EXPIRY_MINUTES` | `60` | Discard notifications that could not be delivered within this window. |

### Telegram

| Variable | Default | What it does |
|---|---|---|
| `TIMEZONE` | `UTC` | IANA timezone (e.g. `Europe/Bratislava`) that a destination's quiet hours are read in. |
| `TELEGRAM_BOT_TOKEN` | unset | From [@BotFather](https://t.me/BotFather). Enables Telegram delivery and the pairing bot. |

### Dashboard

| Variable | Default | What it does |
|---|---|---|
| `WEB_ENABLED` | `true` | The dashboard. Turn it off if you only use the CLI. |
| `WEB_AUTH_TOKEN` | unset | Optional. With no token the dashboard has no sign-in, which is fine while it listens on localhost. **Set one before exposing it further** — it shows your webhook URLs and chat ids. Generate with `openssl rand -hex 32`. |
| `WEB_HOST` | `127.0.0.1` | Loopback by default. Only widen behind a reverse proxy you trust. |
| `WEB_PORT` | `8000` | |
| `WEB_PUBLIC_URL` | unset | The address the dashboard is reachable at from wherever you read your alerts — set it when the dashboard sits behind a reverse proxy or a tunnel. It becomes the Dashboard link in Discord messages; unset, that link points at `http://<WEB_HOST>:<WEB_PORT>`. |

### Logging and development

| Variable | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_FORMAT` | `console` | `json` when something else is collecting the logs. |
| `FETCH_MODE` | `live` | `mock` replays recorded responses from disk instead of calling Vinted. |
| `MOCK_SCENARIO_DIR` | unset | Required when `FETCH_MODE=mock`. |

## Commands

```
vinted-sniper run                      start watching (what the container runs)
vinted-sniper check --url <url>        fetch one search once and print the result
vinted-sniper watch <url> [options]    add a search
vinted-sniper searches                 list searches
vinted-sniper unwatch <id>             remove one
vinted-sniper destination <kind> <target>   add somewhere to send
vinted-sniper destinations             list them
vinted-sniper pair-telegram            print a link that connects a Telegram chat
vinted-sniper status                   how each search is doing
vinted-sniper migrate                  create or update the database, then exit
vinted-sniper heartbeat                exit 0 if the app is alive (the health check)
```

Options for `watch`:

| Option | Meaning |
|---|---|
| `--name` | What to call it. Defaults to the search text. |
| `--every N` | Seconds between checks for this search. |
| `--max-price N` | Skip anything above this **including buyer protection**. |
| `--exclude a,b,c` | Skip listings whose title contains any of these words. |
| `--require a,b` | Only keep listings whose title contains **all** of these words. Vinted's own text search also matches descriptions, which is where most of the noise comes from. |
| `--title-regex PATTERN` | Only keep listings whose title matches this regular expression (case-insensitive). |
| `--min-seller-rating N` | Skip sellers rated below N percent. Sellers with no rating yet are skipped too. |
| `--min-seller-reviews N` | Skip sellers with fewer than N reviews. |
| `--block-seller a,b` | Skip these seller usernames outright. |

The same fields are under "More filters" when adding a search in the dashboard, and every
search has an **Edit** button there for changing them afterwards — the change takes effect
on the next check, no restart needed. Only the URL is fixed: it is what the search *is*.
| `--to 1,2` | Destination ids to notify. Defaults to all active ones. |

## Adding a search

Search on Vinted, set your filters, copy the address bar, and paste that URL into the
dashboard or `vinted-sniper watch`. The dashboard can also build the URL for you, from
Vinted's own categories, brand autocomplete and filters.

Any country site works: `vinted.fr`, `.de`, `.nl`, `.co.uk`, `.com`, and the rest. The site
you copied from is the site it watches, and the links you get back point there too. Tracking
parameters are stripped, so pasting the same search twice counts as one search.

## Destinations

Where to get each kind of target:

| Kind | Setup |
|---|---|
| `discord` | In your server: Settings → Integrations → Webhooks → New Webhook → Copy URL. Nothing to invite, nothing to host. |
| `telegram` | Create a bot with [@BotFather](https://t.me/BotFather), set `TELEGRAM_BOT_TOKEN`, then run `vinted-sniper pair-telegram` and tap the link it prints. It finds your chat id for you. Each alert carries two extra buttons the bot handles itself: skip that seller for the search, and pause the search (`/resume <id>` brings it back). Buttons only work from a paired chat. |
| `ntfy` | Pick a topic name, install the ntfy app. No account. |
| `webhook` | Any URL you control: n8n, Home Assistant, a script. Payload below. |

Each search can go to its own set of destinations, so a Discord channel for one thing and
your phone for another is normal.

A listing that was already announced is announced again when its price drops by
`PRICE_DROP_MIN_PERCENT` or more — Vinted sellers cut prices often, and a jacket that was
too dear on Monday may not be on Thursday. This costs nothing extra: the search's first
page, which the app fetches anyway, carries every listing's current price, so a listing is
tracked for as long as it stays on that page (on a quiet search, indefinitely). Webhook
consumers see `"event": "price_drop"` and `"previous_total_price"` on such items.

A destination can have **quiet hours** — `--quiet 23:00-07:00` on the command line, or the
field next to it in the dashboard — during which nothing is sent. Alerts found meanwhile are
kept (they are exempt from `OUTBOX_EXPIRY_MINUTES`) and go out together when the window
ends; Discord and Telegram fold a large batch into one digest message. Times are read in
`TIMEZONE`.

What is stored per destination — the dashboard and `vinted-sniper destination` fill these in
for you:

| Kind | Fields |
|---|---|
| `discord` | `webhook_url` |
| `telegram` | `chat_id`, optionally `message_thread_id` for a forum topic |
| `ntfy` | `topic`, optionally `server` and `token` |
| `webhook` | `url`, optionally `headers` |

## The webhook payload

A plain webhook destination receives a POST like this. The shape is treated as a contract:
it changes only with a version bump, because other people's automations depend on it.

```json
{
  "version": 1,
  "search": "nike air max",
  "items": [
    {
      "id": 9683334896,
      "site": "vinted.fr",
      "title": "Nike Air Max 90",
      "url": "https://www.vinted.fr/items/9683334896-nike-air-max-90",
      "brand": "Nike",
      "size": "42",
      "condition": "Very good",
      "price": "15.00",
      "total_price": "16.45",
      "currency": "EUR",
      "photo_url": "https://images.vinted.net/...",
      "listed_at": "2026-08-16T20:14:00+00:00",
      "seller": "someone",
      "seller_rating": 0.93,
      "links": {
        "message_seller": "https://www.vinted.fr/items/9683334896/want_it/new",
        "buy": "https://www.vinted.fr/transaction/buy/new?..."
      }
    }
  ]
}
```

`price` is what the seller asks. `total_price` is what you pay. Filters and displays use the
second one.

## RSS

Each search has a feed at `/rss/<search id>.xml`, so with the defaults:

```
http://localhost:8000/rss/1.xml
```

If `WEB_AUTH_TOKEN` is set, feed readers cannot sign in, so the token goes in the URL
instead: `/rss/1.xml?key=<your token>`. Anyone with that URL can read the feed, so treat it
like a password.
