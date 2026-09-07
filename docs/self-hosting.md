# Running it somewhere

*[Slovenská verzia nižšie ↓](#slovensky)*

Two things decide where to put this: it has to stay on, and the connection it makes requests
from matters more than the machine's speed. A Raspberry Pi on your home internet outperforms
a fast server on a blocked address.

## At home (recommended)

Any always-on machine works: a Raspberry Pi 4 or 5, an old laptop, a NAS, a desktop that
doesn't sleep. The app idles at a few percent of one core and a few dozen megabytes.

Why home first:

- Home connections are challenged far less often than datacenter ones by anti-bot systems.
- Nothing to pay, and no free tier that can be withdrawn.
- The database, your webhook URLs and your Telegram token stay on hardware you own.

### On a Raspberry Pi or any Linux box

Install Docker if you don't have it:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in afterwards
```

Then:

```bash
mkdir -p ~/vinted-sniper && cd ~/vinted-sniper
curl -O https://raw.githubusercontent.com/Luc3as/vinted-sniper/luc3as/main/docker-compose.yml
docker compose up -d
```

The dashboard is now on `http://localhost:8000` *of that machine* — searches and webhooks
are set up there, so no `.env` file is needed to start. Grab one only if you want Telegram
or other tuning:

```bash
curl -o .env https://raw.githubusercontent.com/Luc3as/vinted-sniper/luc3as/main/.env.example
```

Images are published for both x86 and ARM, so the same commands work on a Pi.

Check it started:

```bash
docker compose logs -f vinted-sniper
```

`restart: unless-stopped` is already in the compose file, so it comes back after a reboot or
a power cut.

### On a NAS

Synology, Unraid, CasaOS and Umbrel can all run a compose file. The only setting that needs
attention is the volume: keep `/data` on persistent storage, not a scratch disk. For
Portainer, see the next section.

### In Portainer

Portainer runs this as a **stack** — its name for a compose file it manages for you. Two ways
to create one, both under **Stacks → Add stack**:

- **Repository**: point it at `https://github.com/Luc3as/vinted-sniper`, reference
  `refs/heads/luc3as/main`, compose path `docker-compose.yml`. Portainer pulls the compose
  file from the repo, so a redeploy also picks up compose changes.
- **Web editor**: paste a compose file directly. Best when you want to deviate from the
  repo's defaults — a different port, a bind mount, extra environment.

A web-editor stack that matches how this fork runs in production:

```yaml
services:
  vinted-sniper:
    image: ghcr.io/luc3as/vinted-sniper:latest
    container_name: vinted-sniper
    restart: unless-stopped
    environment:
      - VINTED_SNIPER_TELEGRAM_BOT_TOKEN=${TG_TOKEN}
      - VINTED_SNIPER_WEB_AUTH_TOKEN=${WEB_TOKEN}
      - VINTED_SNIPER_HTTP_IMPERSONATE=true
      - VINTED_SNIPER_TIMEZONE=Europe/Bratislava
      - VINTED_SNIPER_POLL_DEFAULT_INTERVAL_S=300
      - VINTED_SNIPER_SITE_REQUESTS_PER_MINUTE=12
      - VINTED_SNIPER_WEB_PUBLIC_URL=http://<host-ip>:8010
      - VINTED_SNIPER_LOG_FORMAT=console
    volumes:
      - vinted-sniper-data:/data
    ports:
      - "8010:8000"   # host port : container port — pick any free host port
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]

volumes:
  vinted-sniper-data:
```

Details that matter:

- **Secrets go in stack environment variables, not the YAML.** Under the editor, add
  `TG_TOKEN` and `WEB_TOKEN` as *Environment variables* on the stack; the compose references
  them as `${TG_TOKEN}`. The YAML stays safe to share, and Portainer keeps the values across
  redeploys.
- **The port mapping publishes the dashboard on the host's LAN address**, unlike the repo's
  compose file, which binds to localhost. That is what you want on a headless NAS — but it
  means anyone on your network can open it, so `WEB_AUTH_TOKEN` is not optional here.
- **`WEB_PUBLIC_URL`** should be the address *you* open the dashboard at (the host's IP and
  the published port). Alert links are built from it.
- **The named volume** survives container replacement and image updates. Your searches,
  destinations and market history live there.

**Updating.** Stack → **Editor** → *Update the stack* with **Re-pull image and redeploy**
enabled pulls the newest `latest` and recreates the container. The database schema migrates
itself at startup. If an update misbehaves, pin the previous digest — see
[troubleshooting.md](troubleshooting.md).

**Logs.** Containers → vinted-sniper → **Logs**. Keep `LOG_COLOR` unset (off): Portainer's
log view renders ANSI colours as noise. The container's health check shows in the container
list — *healthy* means the poll loop is turning.

### On Windows or macOS

Docker Desktop works, but neither machine is likely to stay awake and online reliably.
Disable sleep first, or use something that is always on.

## On a VPS

Reasonable if you have no machine at home. Around €5 a month:

| Provider | Notes |
|---|---|
| Hetzner CX22 | Cheapest sensible option. Their address ranges have a mixed reputation with anti-bot systems, so test before committing. |
| DigitalOcean | Costs a little more, easiest control panel if this is your first server. |
| Contabo | Cheap, generous specs, variable performance. |

Setup is the same as above. Before you commit, run the connection test from the VPS:

```bash
curl -v -c - -L "https://www.vinted.fr/" 2>&1 | grep access_token_web
```

No cookie printed means that address is already being challenged, and you should pick a
different provider rather than fight it.

On a VPS, do not expose port 8000 to the internet directly — the dashboard has no password
unless you set one. Keep the loopback binding in the compose file and reach it through an
SSH tunnel:

```bash
ssh -L 8000:localhost:8000 you@your-server
```

Then open http://localhost:8000 on your own machine. Anything else needs
`VINTED_SNIPER_WEB_AUTH_TOKEN` set *and* a reverse proxy with TLS in front, because the
dashboard can see your webhook URLs.

## Free tiers worth avoiding

**Oracle Cloud Always Free.** Their reclamation policy deletes instances that stay idle, and
a polling bot idles at a few percent CPU by design. They also cut the free ARM allowance in
2026 with little notice. Your instance will eventually disappear.

**Render's free tier.** Free web services sleep after fifteen minutes without traffic, and
background workers are not included in the free plan. This is a background worker.

**Railway and Fly.io.** Both dropped their free tiers. They work fine as paid hosts, at
roughly VPS prices, but don't plan around them being free.

## Backups

Everything lives in one SQLite file inside the `vinted-sniper-data` volume. To copy it out:

```bash
docker compose cp vinted-sniper:/data/app.db ./app-backup.db
```

Losing it costs you your searches and destinations, not much else — listings are pruned after
thirty days anyway.

## Updating

```bash
docker compose pull && docker compose up -d
```

Schema changes are applied automatically at startup. If an update misbehaves, pin the previous
tag in `docker-compose.yml` and open an issue.

## Running without Docker

```bash
git clone https://github.com/Luc3as/vinted-sniper && cd vinted-sniper
uv sync --extra web
uv run vinted-sniper run
```

No `.env` needed here either — copy `.env.example` to `.env` later if you want Telegram or
other tuning.

For a systemd service, point `ExecStart` at `/path/to/.venv/bin/vinted-sniper run`, set
`WorkingDirectory`, and add `Restart=always`. Note that the app handles SIGTERM itself, so
the default `KillSignal` is correct and there is no need for a long `TimeoutStopSec`.

---

<a name="slovensky"></a>

# Kde to nechať bežať (slovensky)

O umiestnení rozhodujú dve veci: musí to zostať zapnuté a pripojenie, z ktorého idú requesty,
je dôležitejšie než rýchlosť stroja. Raspberry Pi na domácom internete porazí rýchly server
na blokovanej adrese.

## Doma (odporúčané)

Funguje akýkoľvek stále zapnutý stroj: Raspberry Pi 4 či 5, starý notebook, NAS, desktop,
ktorý nespí. Appka beží na pár percentách jedného jadra a pár desiatkach megabajtov.

Prečo najprv doma:

- Domáce pripojenia anti-bot systémy challengujú oveľa menej než datacentrové.
- Nič neplatíš a nie je tu free tier, ktorý ti môžu vziať.
- Databáza, tvoje webhook URL a Telegram token zostávajú na hardvéri, ktorý vlastníš.

### Na Raspberry Pi alebo hocijakom Linuxe

Nainštaluj Docker, ak ho nemáš:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # potom sa odhlás a prihlás
```

Potom:

```bash
mkdir -p ~/vinted-sniper && cd ~/vinted-sniper
curl -O https://raw.githubusercontent.com/Luc3as/vinted-sniper/luc3as/main/docker-compose.yml
docker compose up -d
```

Dashboard je teraz na `http://localhost:8000` *toho stroja* — vyhľadávania a webhooky sa
nastavujú tam, takže na štart netreba žiadny `.env`. Stiahni si ho, len ak chceš Telegram
alebo iné ladenie:

```bash
curl -o .env https://raw.githubusercontent.com/Luc3as/vinted-sniper/luc3as/main/.env.example
```

Images sa publikujú pre x86 aj ARM, takže tie isté príkazy fungujú aj na Pi.

Over, že to nabehlo:

```bash
docker compose logs -f vinted-sniper
```

`restart: unless-stopped` je už v compose súbore, takže sa to vráti po reboote aj po výpadku
prúdu.

### Na NASe

Synology, Unraid, CasaOS aj Umbrel vedia spustiť compose súbor. Pozornosť potrebuje jediné
nastavenie: volume — `/data` drž na perzistentnom úložisku, nie na scratch disku. Pre
Portainer pozri ďalšiu sekciu.

### V Portaineri

Portainer to spúšťa ako **stack** — jeho pomenovanie pre compose súbor, ktorý spravuje za
teba. Vytvoriť sa dá dvoma spôsobmi, oba pod **Stacks → Add stack**:

- **Repository**: nasmeruj ho na `https://github.com/Luc3as/vinted-sniper`, referencia
  `refs/heads/luc3as/main`, cesta ku compose `docker-compose.yml`. Portainer si compose súbor
  ťahá z repa, takže redeploy zoberie aj zmeny compose.
- **Web editor**: vlož compose priamo. Najlepšie, keď sa chceš odchýliť od predvolieb repa —
  iný port, bind mount, environment navyše.

Web-editor stack zodpovedajúci tomu, ako tento fork beží v produkcii:

```yaml
services:
  vinted-sniper:
    image: ghcr.io/luc3as/vinted-sniper:latest
    container_name: vinted-sniper
    restart: unless-stopped
    environment:
      - VINTED_SNIPER_TELEGRAM_BOT_TOKEN=${TG_TOKEN}
      - VINTED_SNIPER_WEB_AUTH_TOKEN=${WEB_TOKEN}
      - VINTED_SNIPER_HTTP_IMPERSONATE=true
      - VINTED_SNIPER_TIMEZONE=Europe/Bratislava
      - VINTED_SNIPER_POLL_DEFAULT_INTERVAL_S=300
      - VINTED_SNIPER_SITE_REQUESTS_PER_MINUTE=12
      - VINTED_SNIPER_WEB_PUBLIC_URL=http://<ip-hosta>:8010
      - VINTED_SNIPER_LOG_FORMAT=console
    volumes:
      - vinted-sniper-data:/data
    ports:
      - "8010:8000"   # port hosta : port kontajnera — zvoľ hociktorý voľný port hosta
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]

volumes:
  vinted-sniper-data:
```

Detaily, na ktorých záleží:

- **Tajomstvá patria do environment premenných stacku, nie do YAMLu.** Pod editorom pridaj
  `TG_TOKEN` a `WEB_TOKEN` ako *Environment variables* stacku; compose sa na ne odkazuje cez
  `${TG_TOKEN}`. YAML zostáva bezpečný na zdieľanie a Portainer si hodnoty drží aj cez
  redeploye.
- **Mapovanie portu publikuje dashboard na LAN adrese hosta**, na rozdiel od compose súboru v
  repe, ktorý sa viaže na localhost. Na headless NASe je to presne to, čo chceš — ale znamená
  to, že ho otvorí ktokoľvek na tvojej sieti, takže `WEB_AUTH_TOKEN` tu nie je voliteľný.
- **`WEB_PUBLIC_URL`** má byť adresa, na ktorej dashboard otváraš *ty* (IP hosta a publikovaný
  port). Stavajú sa z nej odkazy v alertoch.
- **Pomenovaný volume** prežije výmenu kontajnera aj update image. Bývajú v ňom tvoje
  vyhľadávania, ciele a trhová história.

**Aktualizácia.** Stack → **Editor** → *Update the stack* so zapnutým **Re-pull image and
redeploy** stiahne najnovší `latest` a znovu vytvorí kontajner. Schéma databázy sa migruje
sama pri štarte. Ak sa update pokazí, pripni predchádzajúci digest — pozri
[troubleshooting.md](troubleshooting.md).

**Logy.** Containers → vinted-sniper → **Logs**. `LOG_COLOR` nechaj nenastavené (vypnuté):
Portainerov náhľad logov renderuje ANSI farby ako šum. Health check kontajnera vidno v
zozname kontajnerov — *healthy* znamená, že poll slučka sa točí.

### Na Windows alebo macOS

Docker Desktop funguje, ale ani jeden z tých strojov pravdepodobne nezostane spoľahlivo hore
a online. Najprv vypni uspávanie, alebo použi niečo, čo je stále zapnuté.

## Na VPS

Rozumné, ak doma stroj nemáš. Okolo 5 € mesačne:

| Poskytovateľ | Poznámky |
|---|---|
| Hetzner CX22 | Najlacnejšia rozumná voľba. Ich rozsahy adries majú u anti-bot systémov zmiešanú povesť, tak otestuj pred záväzkom. |
| DigitalOcean | O čosi drahší, najjednoduchší panel, ak je toto tvoj prvý server. |
| Contabo | Lacný, štedré parametre, premenlivý výkon. |

Setup je rovnaký ako vyššie. Pred záväzkom spusti z VPS test pripojenia:

```bash
curl -v -c - -L "https://www.vinted.fr/" 2>&1 | grep access_token_web
```

Žiadna vypísaná cookie znamená, že adresa je už challengovaná — vyber si iného poskytovateľa
namiesto boja.

Na VPS nevystavuj port 8000 priamo do internetu — dashboard nemá heslo, kým ho nenastavíš.
Nechaj v compose súbore loopback binding a pristupuj cez SSH tunel:

```bash
ssh -L 8000:localhost:8000 ty@tvoj-server
```

Potom otvor http://localhost:8000 na vlastnom stroji. Čokoľvek iné potrebuje nastavený
`VINTED_SNIPER_WEB_AUTH_TOKEN` *a* reverse proxy s TLS pred tým, lebo dashboard vidí tvoje
webhook URL.

## Free tierom sa vyhni

**Oracle Cloud Always Free.** Ich politika reklamácie maže nečinné inštancie a polling bot je
z princípu nečinný na pár percentách CPU. V roku 2026 navyše bez veľkého varovania osekali
free ARM. Tvoja inštancia skôr či neskôr zmizne.

**Render free tier.** Free web services zaspia po pätnástich minútach bez prevádzky a
background workery vo free pláne nie sú. Toto je background worker.

**Railway a Fly.io.** Obaja free tier zrušili. Ako platené hostingy fungujú dobre, zhruba za
cenu VPS, ale neplánuj s tým, že budú zadarmo.

## Zálohy

Všetko žije v jednom SQLite súbore vo volume `vinted-sniper-data`. Skopíruješ ho von takto:

```bash
docker compose cp vinted-sniper:/data/app.db ./app-backup.db
```

Jeho strata ťa stojí vyhľadávania a ciele, nič viac — inzeráty sa aj tak mažú po tridsiatich
dňoch.

## Aktualizácia

```bash
docker compose pull && docker compose up -d
```

Zmeny schémy sa aplikujú automaticky pri štarte. Ak sa update pokazí, pripni predchádzajúci
tag v `docker-compose.yml` a otvor issue.

## Beh bez Dockera

```bash
git clone https://github.com/Luc3as/vinted-sniper && cd vinted-sniper
uv sync --extra web
uv run vinted-sniper run
```

Ani tu netreba `.env` — `.env.example` skopíruj na `.env` neskôr, ak chceš Telegram alebo iné
ladenie.

Pre systemd service nasmeruj `ExecStart` na `/cesta/k/.venv/bin/vinted-sniper run`, nastav
`WorkingDirectory` a pridaj `Restart=always`. Appka si SIGTERM obslúži sama, takže predvolený
`KillSignal` je správny a dlhý `TimeoutStopSec` netreba.
