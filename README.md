<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/logo/wordmark-dark.svg">
  <img src="docs/media/logo/wordmark-light.svg" alt="vinted-sniper — See it first. Know if it's worth it." width="640">
</picture>

*[Slovenská verzia nižšie ↓](#slovensky)*

[![CI](https://github.com/Luc3as/vinted-sniper/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/Luc3as/vinted-sniper/actions/workflows/ci.yml)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Upstream](https://img.shields.io/badge/fork_of-jasp--nerd%2Fvinted--sniper-555)](https://github.com/jasp-nerd/vinted-sniper)

</div>

Paste the URL of a Vinted search. It checks that search every few minutes — anonymously, no
login — and tells you in **Telegram**, Discord, ntfy or a webhook when something new matches.
This fork adds the things you need once you actually run it for a while: it survives Vinted's
anti-bot refusals instead of amplifying them, it knows what a *good* price is from the market it
has seen, it lets an **AI agent** judge each listing from its photos before you see it, and it
explains itself in the dashboard so nobody needs the docs.

```
vinted.sk / .cz / .de …  ──(one request budget per site, browser TLS)──►  poller per search
                                                                                │
        market memory  ◄── every listing on the page, price & how long it stayed ◄┤
        listings       ◄── the ones that passed your filters ───────────────────┤
        outbox         ◄── alerts: new · price drop · verdict  ◄────────────────┘
                              │
                        one worker per destination, paced, quiet hours, digests
                              ├─► Telegram  (buttons: skip seller · pause · 👍/👎)
                              ├─► Discord · ntfy
                              └─► webhook ──► n8n + LLM ──► POST /api/items/{id}/enrichment
                                                              └─► verdict woven into the alert
```

## Why this fork exists

The upstream tool is well built and was the only free notifier still standing after Vinted put
DataDome in front of its catalog API. Running it for a day showed what a single evening of use
cannot: one refused request crashed the search's task, the supervisor restarted it every
fifteen seconds, seven searches loaded the homepage with seven browser identities inside one
second, and the address was blocked within the hour. The buttons under every alert pointed to
Vinted pages that no longer exist. A niche search was "stale" forever because a busy one kept
moving. None of that is visible until you run it, and all of it is fixed here — with tests that
reproduce the production logs.

Then the interesting part: a notifier that finds a jacket at 45 € cannot tell you whether that
is a bargain. What it costs new says little; what the same thing has been *selling for* on
Vinted this month says everything. This fork remembers every price it sees, tells you in words
where a listing sits ("cheaper than 88 % of 312 similar listings seen this month"), lets you
filter on that instead of guessing a number, and — if you plug in the bundled n8n agent — has a
model look at the photos, name the exact product, check the retail price, weigh the seller and
the market, and score the deal before the alert reaches your phone.

## What you get on top of upstream

**It stays up**
- A refusal drops the session and backs off; it never re-bootstraps inside the error handler.
- One handshake per country site, shared; searches start staggered; a request budget bounds the
  address as a whole (`SITE_REQUESTS_PER_MINUTE`).
- One refused search holds every search on that site — shown as **cooling**, kept across
  restarts, announced to your status destination.
- The watchdog judges a quiet search against *its own* rhythm, not its busiest neighbour.

**It knows the market**
- Every listing on every page is a price point. Alerts carry "📊 cheaper than X % of N".
- **Only the cheapest N %** — a filter that follows the season by itself.
- **Price drops**: a listing you saw comes back when it gets cheaper, at zero extra requests.
- **Clone** a search to `.de`, `.pl`, `.cz`, … — same filters, same destinations, far more
  listings. A listing on several sites is alerted once.

**It can think, if you let it**
- Webhook payload carries photos, market position, demand (hearts per hour), retail prices
  found earlier and your 👍/👎 history; the alert waits `ENRICHMENT_WAIT_S` for a verdict.
- Bundled n8n workflow (Claude Sonnet 4.5 via OpenRouter, SerpApi, Google Lens fallback) posts
  back: identified model, retail price, match, authenticity risk, deal score, one sentence.
- 🔥 HOT DEAL / 🤖 / 💤 headlines; 💤 arrives without a sound. Late hot verdicts get a follow-up.

**It is usable by a human**
- Dashboard with `?` on every field, a state legend, explainers, a Help page with a glossary and
  the running settings; success notices, confirmations, dark mode, live refresh.
- `/history`: every alert, where it went, whether it got there and why not.
- Edit a search in place; bulk interval change; quiet hours and health notices per destination.
- Filters Vinted cannot express: title must contain, title regex, seller rating and review
  floor, sellers to skip (also from the alert's **Skip seller** button).
- Alerts and bot replies in **English or Slovak per destination**.
- Monday-morning summary; `export` / `import` of everything you configured.

## Run

```bash
git clone https://github.com/Luc3as/vinted-sniper.git && cd vinted-sniper
docker build -t vinted-sniper:impersonate .        # includes the browser-TLS extra
docker compose up -d                                # or the Portainer stack below
```

Open **http://localhost:8000**, paste a Vinted search URL, add a destination. For Telegram run
`docker exec vinted-sniper vinted-sniper pair-telegram` and tap the link — it finds the chat id
for you. Nothing else is required; everything below is tuning.

### Config that matters (all env, prefix `VINTED_SNIPER_`; full list in [docs/configuration.md](docs/configuration.md))

| env | default | meaning |
|-----|---------|---------|
| `WEB_AUTH_TOKEN` | unset | Dashboard password. Also the Bearer the agent uses to post verdicts. Set it. |
| `HTTP_IMPERSONATE` | `false` | Present a browser's TLS fingerprint. **Set `true`** — plain Python TLS is the first thing DataDome looks at. |
| `SITE_REQUESTS_PER_MINUTE` | `12` | Ceiling per country site, all searches together, homepage loads included. |
| `POLL_DEFAULT_INTERVAL_S` | `60` | 120–600 is sensible; faster finds nothing sooner and gets you blocked. |
| `TIMEZONE` | `UTC` | For quiet hours and the Monday report — e.g. `Europe/Bratislava`. |
| `ENRICHMENT_WAIT_S` | `0` | How long chat alerts wait for the agent's verdict. `0` = no agent. |
| `ENRICHMENT_HIGHLIGHT_SCORE` / `_SILENT_BELOW` | `75` / `40` | Score for 🔥 · score under which the phone stays silent. |
| `PRICE_DROP_MIN_PERCENT` | `10` | Announce again when the total price falls by this much. |
| `WEB_PUBLIC_URL` | unset | Address the agent can reach the dashboard at (the callback URL is built from it). |
| `LOG_COLOR` | `false` | Leave off when Portainer or `docker logs` is the reader. |

### Portainer stack

```yaml
services:
  vinted-sniper:
    image: vinted-sniper:impersonate
    container_name: vinted-sniper
    restart: unless-stopped
    environment:
      - VINTED_SNIPER_TELEGRAM_BOT_TOKEN=${TG_TOKEN}
      - VINTED_SNIPER_WEB_AUTH_TOKEN=${WEB_TOKEN}
      - VINTED_SNIPER_HTTP_IMPERSONATE=true
      - VINTED_SNIPER_TIMEZONE=Europe/Bratislava
      - VINTED_SNIPER_POLL_DEFAULT_INTERVAL_S=300
      - VINTED_SNIPER_ENRICHMENT_WAIT_S=90
      - VINTED_SNIPER_WEB_PUBLIC_URL=http://<host-ip>:8000
    volumes:
      - /opt/AI_PROJECTS/vinted-sniper-data:/data
    ports: ["8000:8000"]
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]
```

### The agent (optional)

Import the n8n workflow described in [docs/enrichment.md](docs/enrichment.md), fill three
credentials (OpenRouter, SerpApi, a Bearer with your `WEB_AUTH_TOKEN`), add its webhook URL as
a **webhook destination** here and route your searches to it. Roughly 2–3 ¢ per listing.

## Docs

- [Configuration](docs/configuration.md) — every setting, the CLI, channels, the webhook payload.
- [Enrichment](docs/enrichment.md) — the contract between this app and any agent you build.
- [Self-hosting](docs/self-hosting.md) · [Troubleshooting](docs/troubleshooting.md) — "nothing
  arrives", 403s, the one-line test that tells you whether your address is challenged.
- [CHANGELOG](CHANGELOG.md) — everything since the fork, with reasons. Upstream's README is kept
  as [README.upstream.md](README.upstream.md).

## Tests

```bash
uv sync --extra web --extra impersonate --group dev
uv run pytest -q            # 340+ tests, including reproductions of the production failures
uv run ruff check src tests && uv run mypy src
```

## License & upstream

MIT, as upstream. Not affiliated with Vinted; it reads public listings anonymously and never
logs in, buys, or lists anything — it cannot buy on your behalf and never will — Vinted's terms prohibit automated access, so running it is your call
([docs/legal.md](docs/legal.md)). The bug fixes here are offered back to
[jasp-nerd/vinted-sniper](https://github.com/jasp-nerd/vinted-sniper). The logo and wordmark are this fork's own and
are **not** MIT — see [docs/media/logo/LICENSE.md](docs/media/logo/LICENSE.md).

---

<a name="slovensky"></a>

<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/media/logo/logo-dark.svg">
  <img src="docs/media/logo/logo-light.svg" alt="" width="96">
</picture>

# vinted-sniper (slovensky)

*Vidíš to prvý. A vieš, či to stojí za to.*
</div>

Vlož URL vyhľadávania z Vintedu. Appka ho každých pár minút skontroluje — anonymne, bez
prihlásenia — a keď sa objaví niečo nové, čo prejde tvojimi filtrami, napíše ti do
**Telegramu**, Discordu, ntfy alebo na webhook. Tento fork pridáva to, čo potrebuješ, keď to
reálne necháš bežať: prežije odmietnutia Vintedu namiesto toho, aby ich zosilňoval, vie, čo je
*dobrá* cena podľa trhu, ktorý videl, nechá **AI agenta** posúdiť inzerát z fotiek skôr, než ho
uvidíš, a v dashboarde sa vysvetľuje sám.

## Prečo fork

Pôvodný nástroj je dobre napísaný a bol jediný bezplatný notifikátor, ktorý prežil DataDome
pred katalógovým API Vintedu. Deň prevádzky ale ukázal, čo jeden večer neukáže: jedno
odmietnutie zhodilo úlohu vyhľadávania, supervisor ju reštartoval každých 15 sekúnd, sedem
vyhľadávaní načítalo homepage so siedmimi identitami prehliadača v jednej sekunde a IP bola do
hodiny blokovaná. Tlačidlá pod alertmi viedli na stránky, ktoré Vinted už nemá. Niche
vyhľadávanie bolo večne „stale", lebo rušné susedné stále nachádzalo. Nič z toho nevidíš, kým to
nespustíš — a všetko je tu opravené, s testami, ktoré reprodukujú produkčné logy.

A potom to zaujímavé: notifikátor, ktorý nájde bundu za 45 €, ti nepovie, či je to výhra. Cena
v obchode povie málo; za koľko sa tá istá vec *predáva na Vintede tento mesiac* povie všetko.
Fork si pamätá každú cenu, ktorú videl, povie ti slovami, kde inzerát sedí („lacnejší ako 88 %
z 312 podobných inzerátov za tento mesiac"), dá ti podľa toho filtrovať namiesto hádania čísla, a
ak zapojíš priložený n8n agent, model si pozrie fotky, pomenuje presný produkt, overí cenu v
obchode, zváži predajcu aj trh a oskóruje deal ešte predtým, než alert dorazí na telefón.

## Čo je navyše oproti upstreamu

- **Nezhasne**: odmietnutie = zahodiť session a ustúpiť; jeden handshake per krajina; rozfázovaný
  štart; rozpočet requestov na adresu; stav **cooling** s upozornením; watchdog meria ticho podľa
  vlastného rytmu vyhľadávania.
- **Pozná trh**: každý inzerát na stránke je cenový bod; alerty nesú „📊 lacnejší ako X % z N";
  filter **Only the cheapest N %**; **zľavy** na už videných inzerátoch bez requestov navyše;
  **klon** vyhľadávania na `.de`, `.pl`, `.cz` s rovnakými filtrami a cieľmi.
- **Vie myslieť**: webhook s fotkami, trhovou pozíciou, dopytom, cache retail cien a tvojimi
  👍/👎; alert počká `ENRICHMENT_WAIT_S` na verdikt; priložený n8n workflow (Claude Sonnet 4.5 cez
  OpenRouter + SerpApi + Google Lens); 🔥 / 🤖 / 💤 hlavičky, 💤 bez zvuku.
- **Dá sa ovládať**: `?` pri každom poli, legenda stavov, Help stránka so slovníkom, `/history`
  („prišiel môj alert?"), editácia vyhľadávania, quiet hours a health notices per cieľ, filtre na
  názov a predajcu, **správy po slovensky alebo anglicky per cieľ**, pondelkový súhrn,
  export/import konfigurácie.

## Spustenie

```bash
git clone https://github.com/Luc3as/vinted-sniper.git && cd vinted-sniper
docker build -t vinted-sniper:impersonate .
docker compose up -d
```

Otvor **http://localhost:8000**, vlož URL vyhľadávania, pridaj cieľ. Pre Telegram spusti
`docker exec vinted-sniper vinted-sniper pair-telegram` a klikni na link — chat id si nájde
sám. Nastavenia, Portainer stack a agenta pozri v anglickej sekcii vyššie; kompletný zoznam je v
[docs/configuration.md](docs/configuration.md), kontrakt pre agenta v
[docs/enrichment.md](docs/enrichment.md).

## Licencia

MIT, ako upstream. Nie je to projekt Vintedu; číta verejné inzeráty anonymne a nikdy sa
neprihlasuje, nenakupuje ani nepredáva. Podmienky Vintedu automatizovaný prístup zakazujú,
prevádzka je na tvoje zváženie ([docs/legal.md](docs/legal.md)).
