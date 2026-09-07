# When it stops finding things

*[Slovenská verzia nižšie ↓](#slovensky)*

Start here: `vinted-sniper status`. It prints every search with a one-word state, when it
last succeeded, and what went wrong if anything did.

```
Running.

nike air max [ok] — vinted.fr
  last successful check: 34s ago
  newest listing seen:   112s ago

carhartt jacket [failing] — vinted.co.uk
  last successful check: 1840s ago
  blocked 12 times, rate limited 0 times
  last error: vinted.co.uk refused the request with 403
```

The state tells you which of the following sections to read.

## "ok" but nothing is arriving

The search is working; it just isn't matching anything. Common causes, in the order worth
checking:

**Your price limit includes buyer protection.** A limit of 20 rejects a listing priced at 18
that costs 20.50 to actually buy. That is the intended behaviour, but it surprises people.
Check what you set with `vinted-sniper searches`.

**The search itself is narrow.** Open the URL in a browser. If Vinted shows nothing new
either, there is nothing wrong.

**Excluded words are catching more than you meant.** Excluding "new" also excludes "brand new
condition" and anything else containing it.

**Everything is a boosted listing.** Paid bumps are skipped, because they are old listings
resurfacing rather than new ones.

## "stale"

The search has stopped seeing new listings while your other searches on the same site are
still finding them. Vinted sometimes serves a catalog that has quietly stopped updating,
which looks like success from the outside.

The app starts a fresh session on its own when it notices (with `WATCHDOG_ACTION=warn` it
only tells you). If it stays stale for more than an hour, restart it:

```bash
docker compose restart vinted-sniper
```

If it persists after that, the search may have genuinely dried up. Compare with the same URL
in a browser.

## "failing" with 403

Vinted is refusing the connection. Almost always this is about where the request comes from,
not what it asked for. Confirm it in one command:

```bash
curl -v -c - -L "https://www.vinted.fr/" 2>&1 | grep access_token_web
```

Use whichever country site you are watching.

**If that prints a cookie line**, your address is fine and the problem is the app — please
open an issue with your logs.

**If it prints nothing**, your address is being challenged, and no setting will fix that.
Options, cheapest first:

1. **Wait.** These are usually temporary. The app backs off on its own.
2. **Slow down.** Raise `VINTED_SNIPER_POLL_DEFAULT_INTERVAL_S` to 120 or 300, or lower
   `VINTED_SNIPER_SITE_REQUESTS_PER_MINUTE`, which caps the address as a whole. Ten searches
   at once from one address is a lot more traffic than one search. When one search is
   refused, the others on that site hold back automatically (`poll.cooling_down` in the
   logs) — that is deliberate, not a second problem.
3. **Move it home.** Residential connections are challenged far less than datacenter ones. A
   Raspberry Pi is enough.
4. **Turn on TLS impersonation.** Set `VINTED_SNIPER_HTTP_IMPERSONATE=true`. This makes
   requests look like a real browser at the connection level rather than like Python. It
   needs the `impersonate` extra installed, and it is not a magic fix — if your address is
   blocked outright, it stays blocked.
5. **Use a proxy.** `VINTED_SNIPER_PROXY_FILE` points at a text file with one proxy URL per
   line. Free proxy lists are already blocked; if you go this route, use residential proxies
   in the same country as the site you are watching. Most people never need this.

## "failing" with 429

You are checking too often for how Vinted feels about your address right now. The app already
waits as long as Vinted asks it to. If it keeps happening, increase the interval — the
listings will still be there.

## "failing" with malformed

Vinted answered with something that is not a catalog. That usually means either an anti-bot
interstitial (see the 403 section) or a change to their API, which needs a fix here. Worth
opening an issue with the logged error.

## Notifications stopped but searches are fine

Check the destination:

```bash
vinted-sniper destinations
```

A destination marked `disabled` was switched off because the other end said it was gone — a
deleted Discord webhook, a blocked Telegram bot, a wrong chat id. This is deliberate:
repeatedly sending to a dead webhook is what gets your address rate-limited by Discord
itself. Remove it and add the new one.

Also worth knowing: undelivered notifications are discarded after an hour by default. If
Discord was down all night, you get the current listings when it comes back rather than a
flood of stale ones.

## It says "Not running"

The heartbeat is stale, meaning the process is not coming round its loop.

```bash
docker compose logs --tail 50 vinted-sniper
docker compose ps
```

If the container is restarting repeatedly, the logs will say why.

Also worth grepping the logs for `web.no_password`: that warning means the dashboard is
listening on an address other than localhost with no sign-in, so anyone who can reach the
port can read your webhook URLs. Either set `VINTED_SNIPER_WEB_AUTH_TOKEN` or check that the
published port is not public.

## Telegram never connects

The bot cannot message you first — that is a Telegram rule, not a limitation here. Run
`vinted-sniper pair-telegram --bot-username yourbot`, then tap the link it prints, in the chat
or group you want alerts in. The app must be running for the link to work, and the link
expires after thirty minutes.

For a group, add the bot to the group first. For a forum topic, tap the link inside that
topic.

## Everything is broken after an update

Roll back to the previous image and open an issue. `latest` moves, so pin the digest of the
image that worked (`docker image ls --digests`):

```yaml
image: ghcr.io/luc3as/vinted-sniper@sha256:<previous digest>
```

The weekly canary tests against the live site, so an outright break usually gets caught
before it ships. If you hit one anyway, the logs from `docker compose logs` are the useful
thing to attach.

---

<a name="slovensky"></a>

# Keď prestane nachádzať veci (slovensky)

Začni tu: `vinted-sniper status`. Vypíše každé vyhľadávanie s jednoslovným stavom, kedy
naposledy uspelo a čo sa pokazilo, ak vôbec niečo.

```
Running.

nike air max [ok] — vinted.fr
  last successful check: 34s ago
  newest listing seen:   112s ago

carhartt jacket [failing] — vinted.co.uk
  last successful check: 1840s ago
  blocked 12 times, rate limited 0 times
  last error: vinted.co.uk refused the request with 403
```

Stav ti povie, ktorú z nasledujúcich sekcií čítať.

## „ok", ale nič nechodí

Vyhľadávanie funguje; len sa nič nezhoduje. Bežné príčiny, v poradí, v akom sa oplatí
kontrolovať:

**Tvoj cenový limit zahŕňa buyer protection.** Limit 20 odmietne inzerát za 18, ktorý v
skutočnosti stojí 20,50. To je zámerné správanie, ale ľudí prekvapuje. Čo máš nastavené,
zistíš cez `vinted-sniper searches`.

**Samotné vyhľadávanie je úzke.** Otvor URL v prehliadači. Ak ani Vinted neukazuje nič nové,
nič nie je pokazené.

**Vylúčené slová chytajú viac, než si chcel.** Vylúčenie „new" vylúči aj „brand new
condition" a všetko ostatné, čo ho obsahuje.

**Všetko sú boostované inzeráty.** Platené bumpy sa preskakujú, lebo sú to staré inzeráty,
ktoré sa vynárajú znova — nie nové.

## „stale"

Vyhľadávanie prestalo vidieť nové inzeráty, zatiaľ čo tvoje ostatné vyhľadávania na tej istej
stránke ich stále nachádzajú. Vinted občas servíruje katalóg, ktorý sa potichu prestal
aktualizovať — zvonku to vyzerá ako úspech.

Appka si sama založí novú session, keď si to všimne (pri `WATCHDOG_ACTION=warn` len upozorní).
Ak to zostane stale viac než hodinu, reštartuj:

```bash
docker compose restart vinted-sniper
```

Ak to pretrváva aj potom, vyhľadávanie mohlo naozaj vyschnúť. Porovnaj s tou istou URL v
prehliadači.

## „failing" so 403

Vinted odmieta spojenie. Takmer vždy ide o to, odkiaľ request prichádza, nie o to, čo žiadal.
Over si to jedným príkazom:

```bash
curl -v -c - -L "https://www.vinted.fr/" 2>&1 | grep access_token_web
```

Použi tú krajinu, ktorú sleduješ.

**Ak sa vypíše riadok s cookie**, tvoja adresa je v poriadku a problém je v appke — otvor
prosím issue s logmi.

**Ak sa nevypíše nič**, tvoja adresa je challenged a žiadne nastavenie to neopraví. Možnosti,
od najlacnejšej:

1. **Počkaj.** Obvykle je to dočasné. Appka sama ustupuje.
2. **Spomaľ.** Zvýš `VINTED_SNIPER_POLL_DEFAULT_INTERVAL_S` na 120 či 300, alebo zníž
   `VINTED_SNIPER_SITE_REQUESTS_PER_MINUTE`, ktoré stropuje adresu ako celok. Desať vyhľadávaní
   naraz z jednej adresy je oveľa viac prevádzky než jedno. Keď je jedno vyhľadávanie
   odmietnuté, ostatné na tej stránke sa samy zdržia (`poll.cooling_down` v logoch) — to je
   zámer, nie druhý problém.
3. **Presuň to domov.** Domáce pripojenia sú challengované oveľa menej než datacentrové.
   Raspberry Pi stačí.
4. **Zapni TLS impersonation.** Nastav `VINTED_SNIPER_HTTP_IMPERSONATE=true`. Requesty budú na
   úrovni spojenia vyzerať ako skutočný prehliadač, nie ako Python. Potrebuje nainštalovanú
   extra `impersonate` a nie je to zázrak — ak je adresa blokovaná natvrdo, blokovaná zostane.
5. **Použi proxy.** `VINTED_SNIPER_PROXY_FILE` ukazuje na textový súbor s jednou proxy URL na
   riadok. Free proxy zoznamy sú už zablokované; ak ideš touto cestou, použi rezidenčné proxy
   v krajine stránky, ktorú sleduješ. Väčšina ľudí to nikdy nepotrebuje.

## „failing" so 429

Kontroluješ príliš často na to, ako sa Vinted práve tvári na tvoju adresu. Appka už čaká
presne toľko, koľko si Vinted pýta. Ak sa to opakuje, zvýš interval — inzeráty tam stále budú.

## „failing" s malformed

Vinted odpovedal niečím, čo nie je katalóg. Obvykle to znamená anti-bot interstitial (pozri
sekciu o 403) alebo zmenu ich API, ktorá potrebuje opravu tu. Oplatí sa otvoriť issue so
zalogovanou chybou.

## Notifikácie prestali, ale vyhľadávania sú v poriadku

Skontroluj cieľ:

```bash
vinted-sniper destinations
```

Cieľ označený `disabled` bol vypnutý, lebo druhá strana povedala, že už neexistuje — zmazaný
Discord webhook, zablokovaný Telegram bot, zlé chat id. Je to zámer: opakované posielanie na
mŕtvy webhook je presne to, za čo ti Discord rate-limitne adresu. Odstráň ho a pridaj nový.

Tiež sa oplatí vedieť: nedoručené notifikácie sa predvolene po hodine zahadzujú. Ak bol
Discord celú noc dole, po návrate dostaneš aktuálne inzeráty, nie záplavu starých.

## Píše „Not running"

Heartbeat je starý — proces neprechádza svojou slučkou.

```bash
docker compose logs --tail 50 vinted-sniper
docker compose ps
```

Ak sa kontajner opakovane reštartuje, logy povedia prečo.

Oplatí sa grepnúť logy aj na `web.no_password`: to varovanie znamená, že dashboard počúva na
inej adrese než localhost bez prihlásenia, takže ktokoľvek, kto dosiahne na port, vidí tvoje
webhook URL. Buď nastav `VINTED_SNIPER_WEB_AUTH_TOKEN`, alebo over, že publikovaný port nie
je verejný.

## Telegram sa nikdy nepripojí

Bot ti nemôže napísať prvý — to je pravidlo Telegramu, nie obmedzenie tu. Spusti
`vinted-sniper pair-telegram --bot-username tvojbot`, potom klikni na vypísaný link v chate
alebo skupine, kde chceš alerty. Appka musí bežať, aby link fungoval, a link expiruje po
tridsiatich minútach.

Pri skupine najprv pridaj bota do skupiny. Pri forum topicu klikni na link v danom topicu.

## Po update je všetko rozbité

Vráť sa na predchádzajúci image a otvor issue. `latest` sa hýbe, takže si pripni digest
image, ktorý fungoval (`docker image ls --digests`):

```yaml
image: ghcr.io/luc3as/vinted-sniper@sha256:<predchádzajúci digest>
```

Týždenný canary testuje proti živej stránke, takže úplné rozbitie sa obvykle chytí skôr, než
sa dostane von. Ak naň aj tak narazíš, užitočná príloha sú logy z `docker compose logs`.
