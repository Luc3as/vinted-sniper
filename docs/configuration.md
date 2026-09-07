# Configuration

*[Slovenská verzia nižšie ↓](#slovensky)*

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
| `HTTP_IMPERSONATE` | `false` | Make requests present a real browser's TLS fingerprint. Needs the `impersonate` extra (already in the Docker image). Plain Python TLS is the first thing DataDome looks at, so **leave this on** unless you cannot install the extra. |
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

### Magic Search

A sweep is a one-off look at stock already listed on Vinted, best matches first — separate
from the searches that keep watching for new listings. The first two settings bound how much
one sweep reads before any of it reaches the AI. The rest point the app at the flow that
turns a sentence like "men's Patagonia jacket size M under 60 eur" into search filters.

| Variable | Default | What it does |
|---|---|---|
| `SWEEP_MAX_PAGES` | `4` | How many pages of best-matching listings one sweep reads. Each page is one more request to Vinted, so raising this makes a sweep slower and heavier. |
| `SWEEP_MAX_ITEMS` | `200` | The most listings one sweep will look at, counted before anything is sent to the AI. A hard stop: it wins over the page count. |
| `MAGIC_WEBHOOK_URL` | unset | The n8n flow that turns plain words into the filters a search needs. Leave it unset and Magic Search stays off — the endpoint says so plainly instead of guessing. See [magic-search.md](magic-search.md). |
| `MAGIC_WEBHOOK_TOKEN` | unset | Sent to that flow as a bearer token, so a stranger who finds the URL cannot use it. Set it only if the flow asks for one. |
| `MAGIC_TIMEOUT_S` | `30` | How long to wait for the flow to answer before giving up. An AI reading a sentence takes a few seconds; a minute means something is wrong. |
| `MAGIC_TRIAGE_WEBHOOK_URL` | unset | The n8n flow that looks at listing photos and says which ones are the thing you asked for. Leave it unset and the judging stages stay off — a sweep stops after the free filters. |
| `MAGIC_VERDICT_WEBHOOK_URL` | unset | The flow that gives a full opinion on the best few finds. Point it at a copy of the enrichment flow. |
| `SWEEP_TRIAGE_BATCH` | `20` | How many listings go to the photo check in one go. |
| `SWEEP_MAX_VERDICTS` | `3` | The most full opinions one sweep will pay for. `0` is a real setting: it means stop after the photo check. |
| `MAGIC_COST_PER_MTOK_IN` | `1.0` | What a million words of input costs in euros. Only an estimate, used when the flow does not report a price of its own — when it does, its figure wins. |
| `MAGIC_COST_PER_MTOK_OUT` | `5.0` | What a million words of answer costs in euros. Only an estimate, used when the flow does not report a price of its own — when it does, its figure wins. |

`SWEEP_MAX_ITEMS` has to be at least `SWEEP_MAX_PAGES`, otherwise a sweep would stop before
finishing even one page and asking for several pages would mean nothing.

All three flows share `MAGIC_WEBHOOK_TOKEN` and `MAGIC_TIMEOUT_S` — one credential and one
patience setting, which is what running all three in the same n8n expects. Setting
`MAGIC_VERDICT_WEBHOOK_URL` without `MAGIC_TRIAGE_WEBHOOK_URL` is refused: full opinions are
picked from what the photo check ranked, so the verdict flow alone has nothing to pick from.
The other way round is fine — the photo check on its own is the cheap setup.

### Telegram

| Variable | Default | What it does |
|---|---|---|
| `WEEKLY_REPORT` | `true` | Every Monday at 08:00 (`TIMEZONE`), send destinations flagged for status notices a few lines: listings found, alerts sent, price drops, the best verdict, the busiest searches. |
| `TIMEZONE` | `UTC` | IANA timezone (e.g. `Europe/Bratislava`) that quiet hours, the "Listed at" time in alerts and the Monday report are read in. |
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
| `LOG_COLOR` | `false` | ANSI colours in console logs. Leave off when the reader is Portainer or `docker logs`; they render the escape codes as repeated words. |
| `LOG_FORMAT` | `console` | `json` when something else is collecting the logs. |
| `FETCH_MODE` | `live` | `mock` replays recorded responses from disk instead of calling Vinted. |
| `MOCK_SCENARIO_DIR` | unset | Required when `FETCH_MODE=mock`. |

## Commands

```
vinted-sniper run                      start watching (what the container runs)
vinted-sniper check --url <url>        fetch one search once and print the result
vinted-sniper sweep <url> [options]    look once through what is already for sale
vinted-sniper sweep <url> --judge      the same, but have the AI check the photos too
vinted-sniper watch <url> [options]    add a search
vinted-sniper searches                 list searches
vinted-sniper unwatch <id>             remove one
vinted-sniper destination <kind> <target>   add somewhere to send
vinted-sniper destinations             list them
vinted-sniper pair-telegram            print a link that connects a Telegram chat
vinted-sniper status                   how each search is doing
vinted-sniper migrate                  create or update the database, then exit
vinted-sniper heartbeat                exit 0 if the app is alive (the health check)
vinted-sniper export                   searches, destinations and routes as JSON
vinted-sniper import <file>            add what an export contains (existing entries untouched)
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
| `--cheapest N` | Only listings priced in the cheapest N% of what this search has seen in the last 30 days (every listing on the page counts, filters or not). Adapts to the market by itself; inactive until ten price points exist. The alert states the position in words: "cheaper than 88% of 312 similar listings seen this month". |
| `--to 1,2` | Destination ids to notify. Defaults to all active ones. |

The same fields are under "More filters" when adding a search in the dashboard, and every
search has an **Edit** button there (which also offers **Clone** — the same search, filters
and destinations on another country site) for changing them afterwards — the change takes effect
on the next check, no restart needed. Only the URL is fixed: it is what the search *is*.

Options for `sweep`:

| Option | Meaning |
|---|---|
| `--pages N` | How many pages to read. Defaults to `SWEEP_MAX_PAGES`. Never more than 10, whatever you ask for: every page is another request to Vinted. Ask for more and the sweep says so and reads 10. |
| `--max-items N` | The most listings to look at. Defaults to `SWEEP_MAX_ITEMS`. Never more than 2000, whatever you ask for, so one run's cost stays knowable. Ask for more and the sweep says so and looks at 2000. |
| `--keyword WORD` | A word that makes a listing a better match. Repeat it for more words. Defaults to the words in the search URL. |

A sweep is a one-off read of stock already on Vinted, best matches first. It is not a
search: nothing is saved to watch, nothing is sent anywhere, and no notification can come
out of it. It prints how many pages it read, how many listings it saw, what it skipped and
why, and the best few it kept.

Keywords rank, they do not filter. Sellers write the same coat as "Patagonia jacket",
"Patagonia bunda" and "Kurtka Patagonia", so a listing missing every one of your words is
still kept — it just sits at the bottom of the list. What does remove a listing is a real
limit: a banned word, your budget, the condition or the seller.

The same run is written to the log as one `sweep.summary` line carrying the page count, the
listing count, the skip reasons and how long it took.

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

A destination has a **language** (`--lang en|sk`, or the selector in the dashboard): its alerts,
bot replies and health notices are rendered in it. A Slovak Telegram chat and an English
Discord server can share one instance. The dashboard itself is English for now.

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
        "message_seller": "https://www.vinted.fr/items/9683334896",
        "buy": "https://www.vinted.fr/items/9683334896",
        "seller": "https://www.vinted.fr/member/12345678"
      }
    }
  ]
}
```

`price` is what the seller asks. `total_price` is what you pay. Filters and displays use the
second one. `message_seller` and `buy` keep their keys for compatibility but both resolve to
the listing page — Vinted removed the deep links they used to point at.

Items also carry the enrichment fields — `event`, `photo_urls`, `seller_reviews`,
`enrichment_url`, `favourites`, `views`, `listed_minutes_ago`, `favourites_per_hour`,
`market`, `known_retail`, `reader_language`, `buyer_feedback`, and `previous_total_price` on
a price drop — documented in [enrichment.md](enrichment.md).

## RSS

Each search has a feed at `/rss/<search id>.xml`, so with the defaults:

```
http://localhost:8000/rss/1.xml
```

If `WEB_AUTH_TOKEN` is set, feed readers cannot sign in, so the token goes in the URL
instead: `/rss/1.xml?key=<your token>`. Anyone with that URL can read the feed, so treat it
like a password.

---

<a name="slovensky"></a>

# Konfigurácia (slovensky)

Dva druhy nastavení, zámerne oddelené.

**Environment premenné** riadia proces: kde je databáza, ako často kontrolovať, ako logovať.
Čítajú sa raz pri štarte a sú vypísané nižšie.

**Vyhľadávania a ciele** žijú v databáze a spravujú sa cez CLI alebo dashboard. Neexistuje
konfiguračný súbor, ktorý by ich vymenúval, takže niet čo synchronizovať a niet otázky,
ktorá kópia vyhráva.

## Environment premenné

Každá premenná má prefix `VINTED_SNIPER_`. Všetky sú voliteľné, kde nie je uvedené inak.
[`.env.example`](../.env.example) má ten istý zoznam s komentármi, pripravený na kopírovanie.

### Úložisko

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `DB_PATH` | `./data/app.db` | Kde žije SQLite súbor. V kontajneri už nastavené na `/data/app.db`. |
| `ITEM_RETENTION_DAYS` | `30` | Zmaž uložené inzeráty staršie než toto. Nič sa tým neposiela znova. |
| `KEEP_RAW_JSON` | `false` | Drž plný API payload každého inzerátu. Hodí sa pri ladení parsovania; ukladá viac dát o predajcoch, než notifikácie potrebujú. |

### Kontrolovanie

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `POLL_DEFAULT_INTERVAL_S` | `60` | Sekundy medzi kontrolami novo pridaného vyhľadávania. Hodnoty per vyhľadávanie ho prebíjajú. Menej než 10 sa odmieta. |
| `FRESHNESS_WINDOW_MIN` | `20` | Ignoruj inzeráty s fotkou staršou než toto. Bráni reštartu prehrať staré výsledky. |
| `FIRST_RUN_MODE` | `silent` | Čo urobí úplne nové vyhľadávanie prvý raz: `silent` nenotifikuje nič, `newest` pošle presne jeden inzerát na overenie doručovania. |
| `PRICE_DROP_MIN_PERCENT` | `10` | Ohlás inzerát znova, keď jeho celková cena klesla aspoň o toľkoto odvtedy, čo bol zaznamenaný. Zadarmo: porovnávajú sa len inzeráty stále na prvej stránke vyhľadávania, z už stiahnutej stránky. `0` to vypína. |
| `REQUEST_TIMEOUT_S` | `15` | Ako dlho čakať na Vinted, kým to s jedným requestom vzdáme. |

Podlaha 10 sekúnd nie je opatrnosť pre opatrnosť: API Vintedu samo mešká minúty za tým, čo
ľudia nahrávajú, niekedy dlhšie, takže rýchlejšie kontroly nenájdu nič skôr a blok si
vyslúžia. Každá notifikácia ukazuje, kedy sa inzerát podľa Vintedu objavil aj kedy bol
nájdený, takže skutočné oneskorenie vidíš sám.

### Ako zostať neblokovaný

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `SITE_REQUESTS_PER_MINUTE` | `12` | Strop requestov na jednu krajinu z tejto adresy, počítané cez všetky vyhľadávania a vrátane načítaní homepage. Keď je jedno vyhľadávanie odmietnuté, všetky na tej stránke čakajú rovnaký backoff — skóruje sa adresa, nie vyhľadávanie. |
| `STARTUP_STAGGER_S` | `20` | Sekundy medzi prvými kontrolami po sebe idúcich vyhľadávaní pri štarte, aby reštart s desiatimi vyhľadávaniami neotvoril desiatimi requestami v jednej sekunde. |
| `SESSION_ROTATE_MINUTES` | `60` | Po tomto čase založ novú anonymnú session. Bloky sledujú vek session viac než frekvenciu requestov. |
| `HTTP_IMPERSONATE` | `false` | Requesty prezentujú TLS odtlačok skutočného prehliadača. Potrebuje extra `impersonate` (v Docker image už je). Čisté Python TLS je prvá vec, na ktorú sa DataDome pozerá, takže **nechaj zapnuté**, pokiaľ extra vieš nainštalovať. |
| `PROXY_FILE` | nenastavené | Cesta k textovému súboru s proxy URL, jedna na riadok (prázdne riadky a `#` komentáre sa ignorujú). Používajú sa postupne; odmietnutá si sadne na desať minút. Ak sedia všetky, requesty idú priamo — radšej než vôbec. Málokedy treba. |

### Všímanie si problémov

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `WATCHDOG_STALE_CYCLES` | `10` | Kontroly bez nového inzerátu, po ktorých sa vyhľadávanie považuje za zaseknuté — ale len ak ostatné vyhľadávania na tej istej stránke stále nachádzajú. |
| `WATCHDOG_ACTION` | `rotate` | `warn` to zaloguje; `rotate` navyše založí novú session. |
| `ENRICHMENT_WAIT_S` | `0` | Podrž chatové notifikácie toľkoto sekúnd, aby externý agent stihol poslať verdikt na `/api/items/{id}/enrichment`. Webhook ciele strieľajú hneď bez ohľadu na to. `0` slučku vypína. Pozri [enrichment.md](enrichment.md). |
| `ENRICHMENT_HIGHLIGHT_SCORE` | `75` | Deal skóre od tejto hodnoty dostane hlavičku hot deal. |
| `ENRICHMENT_SILENT_BELOW` | `40` | Deal skóre pod touto hodnotou, alebo verdikt „nie je to, čo sa hľadalo", sa doručí bez zvuku notifikácie (Telegram). |
| `OUTBOX_EXPIRY_MINUTES` | `60` | Zahoď notifikácie, ktoré sa nepodarilo doručiť v tomto okne. |

### Magic Search

Sweep je jednorazový pohľad na to, čo už na Vintede visí, od najlepšie sediacich inzerátov —
oddelene od vyhľadávaní, ktoré stále striehnu na nové. Prvé dve nastavenia ohraničujú, koľko
toho jeden sweep prečíta predtým, než sa čokoľvek z toho dostane k AI. Zvyšné nasmerujú
aplikáciu na flow, ktorý z vety ako „pánska bunda Patagonia veľkosť M do 60 eur" spraví
filtre vyhľadávania.

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `SWEEP_MAX_PAGES` | `4` | Koľko stránok najlepšie sediacich inzerátov jeden sweep prečíta. Každá stránka je ďalší request na Vinted, takže vyššia hodnota robí sweep pomalším a ťažším. |
| `SWEEP_MAX_ITEMS` | `200` | Najviac inzerátov, na ktoré sa jeden sweep pozrie, počítané ešte predtým, než sa čokoľvek pošle AI. Tvrdý strop: prebíja počet stránok. |
| `MAGIC_WEBHOOK_URL` | nenastavené | n8n flow, ktorý z bežných slov spraví filtre, aké vyhľadávanie potrebuje. Keď ho nenastavíš, Magic Search je vypnutý — endpoint to rovno povie namiesto hádania. Pozri [magic-search.md](magic-search.md). |
| `MAGIC_WEBHOOK_TOKEN` | nenastavené | Posiela sa tomu flowu ako bearer token, aby ho cudzí človek, ktorý natrafí na URL, nemohol používať. Nastav ho len vtedy, ak si ho flow pýta. |
| `MAGIC_TIMEOUT_S` | `30` | Ako dlho čakať na odpoveď flowu, kým to vzdáme. AI prečíta vetu za pár sekúnd; minúta znamená, že je niečo zle. |
| `MAGIC_TRIAGE_WEBHOOK_URL` | nenastavené | n8n flow, ktorý sa pozrie na fotky inzerátov a povie, ktoré z nich sú to, čo si hľadal. Keď ho nenastavíš, posudzovacie kroky sú vypnuté — sweep skončí po bezplatných filtroch. |
| `MAGIC_VERDICT_WEBHOOK_URL` | nenastavené | Flow, ktorý dá plný názor na tých pár najlepších nálezov. Nasmeruj ho na kópiu enrichment flowu. |
| `SWEEP_TRIAGE_BATCH` | `20` | Koľko inzerátov ide na kontrolu fotiek naraz. |
| `SWEEP_MAX_VERDICTS` | `3` | Najviac plných názorov, ktoré jeden sweep zaplatí. `0` je platné nastavenie: znamená skončiť po kontrole fotiek. |
| `MAGIC_COST_PER_MTOK_IN` | `1.0` | Koľko stojí milión slov na vstupe v eurách. Len odhad, použije sa vtedy, keď flow sám nenahlási cenu — keď ju nahlási, platí jeho číslo. |
| `MAGIC_COST_PER_MTOK_OUT` | `5.0` | Koľko stojí milión slov odpovede v eurách. Len odhad, použije sa vtedy, keď flow sám nenahlási cenu — keď ju nahlási, platí jeho číslo. |

`SWEEP_MAX_ITEMS` musí byť aspoň `SWEEP_MAX_PAGES`, inak by sweep skončil skôr, než dočíta
čo i len jednu stránku, a pýtať si viac stránok by nedávalo zmysel.

Všetky tri flowy zdieľajú `MAGIC_WEBHOOK_TOKEN` a `MAGIC_TIMEOUT_S` — jedny prihlasovacie
údaje a jedno nastavenie trpezlivosti, čo je presne to, čo čaká niekto, kto beží všetky tri
v jednom n8n. Nastaviť `MAGIC_VERDICT_WEBHOOK_URL` bez `MAGIC_TRIAGE_WEBHOOK_URL` aplikácia
odmietne: plné názory sa vyberajú z toho, čo zoradila kontrola fotiek, takže samotný verdict
flow nemá z čoho vyberať. Naopak je to v poriadku — samotná kontrola fotiek je tá lacná
možnosť.

### Telegram

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `WEEKLY_REPORT` | `true` | Každý pondelok o 08:00 (`TIMEZONE`) pošli cieľom označeným na stavové správy pár riadkov: nájdené inzeráty, odoslané alerty, zľavy, najlepší verdikt, najrušnejšie vyhľadávania. |
| `TIMEZONE` | `UTC` | IANA časová zóna (napr. `Europe/Bratislava`), v ktorej sa čítajú quiet hours, čas „Pridané o" v alertoch a pondelkový report. |
| `TELEGRAM_BOT_TOKEN` | nenastavené | Od [@BotFather](https://t.me/BotFather). Zapína doručovanie do Telegramu a párovacieho bota. |

### Dashboard

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `WEB_ENABLED` | `true` | Dashboard. Vypni, ak používaš iba CLI. |
| `WEB_AUTH_TOKEN` | nenastavené | Voliteľné. Bez tokenu dashboard nemá prihlásenie, čo je v poriadku, kým počúva na localhoste. **Nastav ho skôr, než ho vystavíš ďalej** — ukazuje tvoje webhook URL a chat id. Vygeneruj cez `openssl rand -hex 32`. |
| `WEB_HOST` | `127.0.0.1` | Predvolene loopback. Rozširuj len za reverse proxy, ktorej veríš. |
| `WEB_PORT` | `8000` | |
| `WEB_PUBLIC_URL` | nenastavené | Adresa, na ktorej je dashboard dosiahnuteľný odtiaľ, kde čítaš alerty — nastav, keď dashboard sedí za reverse proxy alebo tunelom. Stane sa z nej Dashboard odkaz v Discord správach; nenastavená, odkaz mieri na `http://<WEB_HOST>:<WEB_PORT>`. |

### Logovanie a vývoj

| Premenná | Predvolené | Čo robí |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_COLOR` | `false` | ANSI farby v konzolových logoch. Nechaj vypnuté, keď je čitateľom Portainer alebo `docker logs`; escape kódy renderujú ako opakované slová. |
| `LOG_FORMAT` | `console` | `json`, keď logy zbiera niečo iné. |
| `FETCH_MODE` | `live` | `mock` prehráva nahraté odpovede z disku namiesto volania Vintedu. |
| `MOCK_SCENARIO_DIR` | nenastavené | Povinné pri `FETCH_MODE=mock`. |

## Príkazy

```
vinted-sniper run                      spusti sledovanie (to, čo beží v kontajneri)
vinted-sniper check --url <url>        stiahni jedno vyhľadávanie raz a vypíš výsledok
vinted-sniper sweep <url> [voľby]      pozri sa raz na to, čo je už na predaj
vinted-sniper sweep <url> --judge      to isté, ale nech AI skontroluje aj fotky
vinted-sniper watch <url> [voľby]      pridaj vyhľadávanie
vinted-sniper searches                 vypíš vyhľadávania
vinted-sniper unwatch <id>             odstráň jedno
vinted-sniper destination <druh> <cieľ>   pridaj, kam posielať
vinted-sniper destinations             vypíš ich
vinted-sniper pair-telegram            vypíš link, ktorý pripojí Telegram chat
vinted-sniper status                   ako sa darí každému vyhľadávaniu
vinted-sniper migrate                  vytvor alebo aktualizuj databázu a skonči
vinted-sniper heartbeat                exit 0, ak appka žije (health check)
vinted-sniper export                   vyhľadávania, ciele a trasy ako JSON
vinted-sniper import <súbor>           pridaj, čo export obsahuje (existujúce sa nedotknú)
```

Voľby pre `watch`:

| Voľba | Význam |
|---|---|
| `--name` | Ako to volať. Predvolene hľadaný text. |
| `--every N` | Sekundy medzi kontrolami tohto vyhľadávania. |
| `--max-price N` | Preskoč všetko nad touto sumou **vrátane buyer protection**. |
| `--exclude a,b,c` | Preskoč inzeráty, ktorých názov obsahuje ktorékoľvek z týchto slov. |
| `--require a,b` | Nechaj len inzeráty, ktorých názov obsahuje **všetky** tieto slová. Textové hľadanie Vintedu matchuje aj popisy, odkiaľ ide väčšina šumu. |
| `--title-regex VZOR` | Nechaj len inzeráty, ktorých názov matchuje tento regulárny výraz (bez ohľadu na veľkosť písmen). |
| `--min-seller-rating N` | Preskoč predajcov s hodnotením pod N percent. Predajcovia zatiaľ bez hodnotenia sa preskakujú tiež. |
| `--min-seller-reviews N` | Preskoč predajcov s menej než N recenziami. |
| `--block-seller a,b` | Preskoč týchto predajcov rovno. |
| `--cheapest N` | Len inzeráty s cenou v najlacnejších N % z toho, čo toto vyhľadávanie videlo za posledných 30 dní (počíta sa každý inzerát na stránke, filtre-nefiltre). Prispôsobuje sa trhu sám; neaktívny, kým nie je desať cenových bodov. Alert pozíciu vysloví slovami: „lacnejší ako 88 % z 312 podobných inzerátov za tento mesiac". |
| `--to 1,2` | Id cieľov na notifikovanie. Predvolene všetky aktívne. |

Tie isté polia sú pod „More filters" pri pridávaní vyhľadávania v dashboarde a každé
vyhľadávanie tam má tlačidlo **Edit** (ktoré ponúka aj **Clone** — to isté vyhľadávanie,
filtre a ciele na inej krajine) na neskoršie zmeny — zmena platí od najbližšej kontroly, bez
reštartu. Pevná je len URL: tá je tým, čím vyhľadávanie *je*.

Voľby pre `sweep`:

| Voľba | Význam |
|---|---|
| `--pages N` | Koľko stránok prečítať. Predvolene `SWEEP_MAX_PAGES`. Nikdy nie viac ako 10, nech si pýtaš čokoľvek: každá stránka je ďalšia požiadavka na Vinted. Ak si vypýtaš viac, sweep to povie a prečíta 10. |
| `--max-items N` | Najviac inzerátov, na ktoré sa pozrieť. Predvolene `SWEEP_MAX_ITEMS`. Nikdy nie viac ako 2000, nech si pýtaš čokoľvek, aby cena jedného behu ostala známa. Ak si vypýtaš viac, sweep to povie a pozrie sa na 2000. |
| `--keyword SLOVO` | Slovo, ktoré robí inzerát lepšie sediacim. Zopakuj ho pre viac slov. Predvolene slová z URL vyhľadávania. |

Sweep je jednorazové prečítanie toho, čo už na Vintede visí, od najlepšie sediacich. Nie je
to vyhľadávanie: nič sa neuloží na sledovanie, nikam sa nič nepošle a žiadna notifikácia z
toho vyjsť nemôže. Vypíše, koľko stránok prečítal, koľko inzerátov videl, čo preskočil a
prečo, a tých pár najlepších, ktoré nechal.

Kľúčové slová radia, nefiltrujú. Predajcovia ten istý kabát napíšu ako „Patagonia jacket",
„Patagonia bunda" aj „Kurtka Patagonia", takže inzerát bez jediného tvojho slova zostáva —
len sedí na konci zoznamu. Inzerát odstráni až skutočné obmedzenie: zakázané slovo, tvoj
rozpočet, stav alebo predajca.

Ten istý beh ide do logu ako jeden riadok `sweep.summary` s počtom stránok, počtom
inzerátov, dôvodmi preskočenia a tým, ako dlho to trvalo.

## Pridanie vyhľadávania

Vyhľadaj na Vintede, nastav filtre, skopíruj adresný riadok a vlož tú URL do dashboardu alebo
do `vinted-sniper watch`. Dashboard ti URL vie aj postaviť — z Vintedových vlastných
kategórií, brand autocomplete a filtrov.

Funguje ktorákoľvek krajina: `vinted.fr`, `.de`, `.nl`, `.co.uk`, `.com` a ostatné. Sleduje
sa stránka, z ktorej si kopíroval, a odkazy, ktoré dostaneš, mieria tam. Trackovacie
parametre sa odstraňujú, takže dvakrát vložené to isté vyhľadávanie sa počíta ako jedno.

## Ciele

Kde vziať jednotlivé druhy cieľov:

| Druh | Nastavenie |
|---|---|
| `discord` | Vo svojom serveri: Settings → Integrations → Webhooks → New Webhook → Copy URL. Nič netreba pozývať, nič hostovať. |
| `telegram` | Vytvor bota cez [@BotFather](https://t.me/BotFather), nastav `TELEGRAM_BOT_TOKEN`, spusti `vinted-sniper pair-telegram` a klikni na vypísaný link. Chat id nájde za teba. Každý alert nesie dve tlačidlá navyše, ktoré bot obsluhuje sám: preskočiť predajcu pre dané vyhľadávanie a pozastaviť vyhľadávanie (`/resume <id>` ho vráti). Tlačidlá fungujú len z párovaného chatu. |
| `ntfy` | Vyber si názov topicu, nainštaluj ntfy appku. Bez účtu. |
| `webhook` | Akákoľvek URL pod tvojou kontrolou: n8n, Home Assistant, skript. Payload nižšie. |

Každé vyhľadávanie môže mať vlastnú sadu cieľov, takže Discord kanál na jedno a telefón na
druhé je normálka.

Už ohlásený inzerát sa ohlási znova, keď jeho cena klesne o `PRICE_DROP_MIN_PERCENT` alebo
viac — predajcovia na Vintede zľavňujú často a bunda pridrahá v pondelok nemusí byť pridrahá
vo štvrtok. Nestojí to nič navyše: prvá stránka vyhľadávania, ktorú appka aj tak sťahuje,
nesie aktuálnu cenu každého inzerátu, takže inzerát sa sleduje, kým na nej zostáva (na tichom
vyhľadávaní donekonečna). Webhook konzumenti na takých položkách vidia `"event":
"price_drop"` a `"previous_total_price"`.

Cieľ má **jazyk** (`--lang en|sk`, alebo selektor v dashboarde): jeho alerty, odpovede bota a
zdravotné správy sa renderujú v ňom. Slovenský Telegram chat a anglický Discord server môžu
zdieľať jednu inštanciu. Samotný dashboard je zatiaľ po anglicky.

Cieľ môže mať **quiet hours** — `--quiet 23:00-07:00` na príkazovom riadku, alebo pole vedľa
v dashboarde — počas ktorých sa nič neposiela. Alerty nájdené medzitým sa držia (sú vyňaté z
`OUTBOX_EXPIRY_MINUTES`) a odídu spolu, keď okno skončí; Discord a Telegram veľkú dávku
zbalia do jednej digest správy. Časy sa čítajú v `TIMEZONE`.

Čo sa ukladá per cieľ — dashboard aj `vinted-sniper destination` to vyplnia za teba:

| Druh | Polia |
|---|---|
| `discord` | `webhook_url` |
| `telegram` | `chat_id`, voliteľne `message_thread_id` pre forum topic |
| `ntfy` | `topic`, voliteľne `server` a `token` |
| `webhook` | `url`, voliteľne `headers` |

## Webhook payload

Obyčajný webhook cieľ dostane POST ako tento. Tvar sa berie ako kontrakt: mení sa len so
zmenou verzie, lebo závisia od neho automatizácie iných ľudí.

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
        "message_seller": "https://www.vinted.fr/items/9683334896",
        "buy": "https://www.vinted.fr/items/9683334896",
        "seller": "https://www.vinted.fr/member/12345678"
      }
    }
  ]
}
```

`price` je, čo si predajca pýta. `total_price` je, čo zaplatíš. Filtre aj zobrazenia
používajú to druhé. `message_seller` a `buy` si nechávajú kľúče kvôli kompatibilite, ale oba
vedú na stránku inzerátu — hlboké odkazy, na ktoré mierili, Vinted odstránil.

Položky nesú aj enrichment polia — `event`, `photo_urls`, `seller_reviews`,
`enrichment_url`, `favourites`, `views`, `listed_minutes_ago`, `favourites_per_hour`,
`market`, `known_retail`, `reader_language`, `buyer_feedback` a `previous_total_price` pri
zľave — zdokumentované v [enrichment.md](enrichment.md).

## RSS

Každé vyhľadávanie má feed na `/rss/<id vyhľadávania>.xml`, takže s predvoľbami:

```
http://localhost:8000/rss/1.xml
```

Ak je nastavený `WEB_AUTH_TOKEN`, čítačky feedov sa nevedia prihlásiť, takže token ide do
URL: `/rss/1.xml?key=<tvoj token>`. Ktokoľvek s tou URL vie feed čítať, tak s ňou zaobchádzaj
ako s heslom.
