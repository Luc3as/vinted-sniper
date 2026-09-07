# Magic Search: typing what you want instead of filling in filters

*[Slovenská verzia nižšie ↓](#slovensky)*

Vinted's own search is a form: a category, a brand, a size, a price ceiling, each one an id
buried in a drop-down. Magic Search lets you skip the form. You type the sentence you would
say out loud — "men's Patagonia Torrentshell jacket, size M, under 60 eur" — an n8n flow
turns it into those ids, and the app checks every one of them against Vinted before it
becomes a search.

That check is the point. A language model asked for a category id always answers with a
number, and an invented number looks exactly like a real one. Vinted does not complain
about it either: a search for a category that does not exist simply comes back empty. So
the app confirms each id against Vinted's own lists first, and a wrong one turns into a
sentence you can read instead of a search that finds nothing.

The prompt itself lives in the flow, not in this repo. This page is the contract between
the two: what the app sends, what it needs back, and what it refuses.

## Setting it up

1. Build the flow in n8n: a Webhook node, a model that reads the sentence, and a Respond
   node that answers with the JSON below. Nothing else about the flow matters to the app.
2. Point the app at it and restart:

   ```
   VINTED_SNIPER_MAGIC_WEBHOOK_URL=https://n8n.example.com/webhook/magic-search
   VINTED_SNIPER_MAGIC_WEBHOOK_TOKEN=<the same token the flow checks>
   VINTED_SNIPER_MAGIC_TIMEOUT_S=30
   ```

3. Try it:

   ```
   curl -X POST http://localhost:8000/api/magic-search/map \
     -H 'Authorization: Bearer <WEB_AUTH_TOKEN>' \
     -H 'Content-Type: application/json' \
     -d '{"text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}'
   ```

Leaving `MAGIC_WEBHOOK_URL` unset simply turns the feature off: the endpoint answers `503`
with "Magic Search is not set up" rather than guessing at a flow that isn't there. The
token is optional — set it only if your flow checks one — and the timeout is how long the
app waits for an answer before giving up. All three are in
[configuration.md](configuration.md).

## What the flow receives

One `POST` to `MAGIC_WEBHOOK_URL`, with `Authorization: Bearer <MAGIC_WEBHOOK_TOKEN>` when
a token is set, and this body:

```json
{"version": 1, "text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}
```

| Field | Meaning |
|---|---|
| `version` | Always `1` today. It exists so the flow can tell an old app from a new one if the body ever changes. |
| `text` | What the person typed, unchanged. At most 500 characters. |
| `tld` | Which country site to map against: `sk`, `cz`, `de`, … Category and size ids differ per site, so the flow must map for the site it is told, not for a default one. |

## What the flow must answer

A single JSON object, `200`. `{"output": {…}}` or `{"result": {…}}` also works — if the
whole body is one of those two keys wrapping an object, the app unwraps it, because that is
how an n8n node's output usually arrives.

Every field is optional. An answer of `{}` is valid and means "no filters I am sure of" —
a perfectly good, very wide search. Fields the app does not know are ignored rather than
rejected, so the flow can start answering with something new before the app is redeployed.
Everything else is strict: a field that is present must be the right shape.

| Field | Type | Required | What it means |
|---|---|---|---|
| `catalog` | `{"id": number, "name": text}` | no | The Vinted category. `id` is at least 1, `name` at most 200 characters. |
| `brand` | `{"id": number, "name": text}` | no | The brand, same shape. |
| `sizes` | list of `{"id", "name"}`, up to 10 | no | Sizes inside that category. Only meaningful with a `catalog` — see below. |
| `price_to` | number, 0 or more | no | Price ceiling, in `currency`. There is no `price_from`: the app searches upward from nothing. |
| `currency` | text, up to 3 characters | no | `EUR`, `CZK`, … Whatever `price_to` is counted in. |
| `search_text` | text, up to 200 characters | no | Free words handed to Vinted's own search box, on top of the filters. |
| `keywords` | list of text, up to 10 | no | The words that matter in a title. The sweep ranks its results by these; it never filters on them. |
| `visual_signature` | text, up to 1000 characters | no | A short description of what the piece *looks like*, for comparing against photos. |
| `watch_hints` | `{"required_keywords": list of text (up to 10), "title_pattern": text or null}` | no | Title rules kept for later, if this sweep is ever promoted into a standing watch. Never used as a search filter. |

### Why every id comes with a name

`catalog`, `brand` and `sizes` are `{id, name}` pairs, not bare numbers, and that redundancy
is deliberate. A number on its own can only be checked for existence. A number *and* the
name the model believed it meant can be checked against each other — and when they
disagree, the app can say **which brand it actually asked for** instead of "brand 90804 is
wrong".

It is also what makes the check possible at all. Vinted has no "does this brand id exist"
endpoint; brands and sizes can only be looked up by the text a person would type. Without
the name there is nothing to look up, so an id with no name could never be confirmed.

### A worked example

The flow receives:

```json
{"version": 1, "text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}
```

and answers:

```json
{
  "catalog": {"id": 2052, "name": "Jackets & Coats"},
  "brand": {"id": 90804, "name": "Patagonia"},
  "sizes": [{"id": 208, "name": "M"}],
  "price_to": 60,
  "currency": "EUR",
  "search_text": "Patagonia Torrentshell",
  "keywords": ["torrentshell"],
  "visual_signature": "lightweight hooded rain shell, plain colour, two zipped hand pockets, small logo on the left chest, no insulation or quilting",
  "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": null}
}
```

The app confirms all four ids, then answers the caller:

```json
{
  "params": {
    "catalog_ids": "2052",
    "brand_ids": "90804",
    "size_ids": "208",
    "price_to": "60",
    "currency": "EUR",
    "search_text": "Patagonia Torrentshell",
    "order": "newest_first"
  },
  "tld": "sk",
  "keywords": ["torrentshell"],
  "visual_signature": "lightweight hooded rain shell, …",
  "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": null},
  "labels": {"catalog": "Jackets & Coats", "brand": "Patagonia", "sizes": ["M"]}
}
```

`params` is the search itself — the exact dictionary a saved search stores and a request to
Vinted takes. `labels` carries the names back so a confirmation screen can say
"Jackets & Coats / Patagonia / M" rather than three integers, and `keywords`,
`visual_signature` and `watch_hints` sit *beside* `params`, never inside it: they describe
how to rank and read the results, and putting them in the search would narrow a sweep meant
to see everything.

## What gets rejected

The ids are checked in three steps, cheapest first, and the first failure stops the rest.
Anything refused comes back as `422` with `{"error": "<the sentence below>"}`.

1. **The category**, against the tree of Vinted categories the app keeps for a week. Almost
   always zero requests to Vinted, and it catches the thing a model most often invents.
   An id nobody has:

   ```
   Vinted has no category 9999 ('Jackets & Coats')
   ```

2. **The brand**, looked up by the name the flow supplied, inside the confirmed category. A
   category's brand list only holds brands it currently has items for, so an empty answer
   is retried across the whole site before anything is called wrong. When it is wrong, the
   near misses come along:

   ```
   Vinted has no brand 90804 ('Patagonya') — searching for 'Patagonya' found: Patagonia, Patagucci
   ```

3. **The sizes**, against the size list of that category. "M" is a different id for a
   jacket than for a shoe, so a size with no category cannot be checked at all and is
   refused rather than passed through:

   ```
   category 2052 has no size 999 ('XM')
   sizes can only be checked inside a category, and this search has none: 208 ('M')
   ```

Two other answers are possible and mean something different. `422` also covers the flow
itself failing — not answering in time, being unreachable, answering with something that is
not JSON, or answering in a shape the app cannot use ("the mapper answered with an unusable
shape: catalog.id: Input should be greater than or equal to 1"). `502` means Vinted itself
could not be reached, so the ids could not be checked; the sentence ends with "the ids in
this search could not be checked". Rewording helps with the first, only waiting helps with
the second.

A rejection is safe and costs nothing beyond the one call that produced it. Nothing was
searched, nothing was stored, and the same sentence — or a better one — can be sent again
straight away.

## Writing the prompt

The prompt lives in your flow, so this is advice rather than contract. It is what the
existing enrichment flow needed:

- **Name the output language explicitly.** Do not assume the model will answer in the
  language of the question. Slovak and Czech are close enough that a model drifts between
  them mid-sentence, so say which one you want *and* give two or three example words in it.
  This matters for `visual_signature` most, since that is prose a person reads.
- **Ask for ids the model is sure about, `null` otherwise.** A missing brand costs a wider
  search; an invented one costs a rejection and a second attempt. Say so in the prompt —
  models will guess rather than leave a field empty unless told not to.
- **Give the model the site it is mapping for.** `tld` is in every request. Ids differ per
  country site, and a category id borrowed from another site fails the first check.
- **`search_text` is a hint, not a filter.** Vinted treats it loosely and will return items
  whose titles do not contain those words at all. Anything that must really be in the title
  belongs in `keywords` (for ranking) or `watch_hints.required_keywords` (for a later
  standing watch).
- **Keep `visual_signature` to what the piece looks like.** Shape, colour, closures,
  pockets, logo placement — things visible in a photo. Not the brand's history, not the
  price, not who wears it. It is compared against thumbnails, so anything unphotographable
  is noise.

## What it costs

One call to the flow per search, and one language-model call inside it. That is why the app
never retries a failed mapping: a silent second attempt would double both the wait and the
bill for what is almost certainly the same failure. A mapping that fails comes straight back
with the reason, and pressing the button again is your decision, not the app's.

---

<a name="slovensky"></a>

# Magic Search: napíš, čo chceš, namiesto vypĺňania filtrov (slovensky)

Vintedské vyhľadávanie je formulár: kategória, značka, veľkosť, cenový strop — a každé z
toho je id zahrabané v rozbaľovacom zozname. Magic Search ti dovolí formulár preskočiť.
Napíšeš vetu, ktorú by si povedal nahlas — „pánska bunda Patagonia Torrentshell, veľkosť M,
do 60 eur" — n8n flow z nej spraví tie id a aplikácia každé jedno overí u Vintedu skôr, než
sa z nich stane vyhľadávanie.

To overenie je celý point. Jazykový model požiadaný o id kategórie vždy odpovie číslom a
vymyslené číslo vyzerá presne ako skutočné. Vinted sa neozve ani on: vyhľadávanie v
neexistujúcej kategórii sa jednoducho vráti prázdne. Takže aplikácia najprv každé id
skonfrontuje s Vintedovými vlastnými zoznamami a zo zlého id sa stane veta, ktorú si
prečítaš, namiesto vyhľadávania, ktoré nič nenájde.

Samotný prompt býva vo flowe, nie v tomto repozitári. Táto stránka je zmluva medzi nimi: čo
aplikácia posiela, čo potrebuje späť a čo odmietne.

## Nastavenie

1. Postav flow v n8n: Webhook node, model, ktorý prečíta vetu, a Respond node, ktorý
   odpovie JSONom nižšie. Nič iné z flowu aplikáciu nezaujíma.
2. Nasmeruj naň aplikáciu a reštartuj:

   ```
   VINTED_SNIPER_MAGIC_WEBHOOK_URL=https://n8n.example.com/webhook/magic-search
   VINTED_SNIPER_MAGIC_WEBHOOK_TOKEN=<ten istý token, aký kontroluje flow>
   VINTED_SNIPER_MAGIC_TIMEOUT_S=30
   ```

3. Vyskúšaj:

   ```
   curl -X POST http://localhost:8000/api/magic-search/map \
     -H 'Authorization: Bearer <WEB_AUTH_TOKEN>' \
     -H 'Content-Type: application/json' \
     -d '{"text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}'
   ```

Keď `MAGIC_WEBHOOK_URL` nenastavíš, funkcia je jednoducho vypnutá: endpoint odpovie `503` a
„Magic Search is not set up" namiesto hádania o flowe, ktorý neexistuje. Token je voliteľný
— nastav ho, len ak ho tvoj flow kontroluje — a timeout je, ako dlho aplikácia čaká na
odpoveď, kým to vzdá. Všetky tri sú v [configuration.md](configuration.md).

## Čo flow dostane

Jeden `POST` na `MAGIC_WEBHOOK_URL`, s hlavičkou `Authorization: Bearer
<MAGIC_WEBHOOK_TOKEN>`, keď je token nastavený, a s týmto telom:

```json
{"version": 1, "text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}
```

| Pole | Význam |
|---|---|
| `version` | Dnes vždy `1`. Existuje preto, aby flow vedel rozlíšiť starú aplikáciu od novej, keby sa telo niekedy zmenilo. |
| `text` | Čo človek napísal, nezmenené. Najviac 500 znakov. |
| `tld` | Pre ktorú krajinu mapovať: `sk`, `cz`, `de`, … Id kategórií a veľkostí sa medzi stránkami líšia, takže flow musí mapovať pre stránku, ktorú dostal, nie pre nejakú predvolenú. |

## Čo musí flow odpovedať

Jeden JSON objekt, `200`. Funguje aj `{"output": {…}}` alebo `{"result": {…}}` — keď je celé
telo jeden z týchto dvoch kľúčov obaľujúcich objekt, aplikácia ho rozbalí, lebo takto výstup
n8n nodu obvykle prichádza.

Každé pole je voliteľné. Odpoveď `{}` je platná a znamená „žiadne filtre, ktorými by som si
bol istý" — úplne v poriadku, len veľmi široké vyhľadávanie. Polia, ktoré aplikácia nepozná,
ignoruje namiesto odmietnutia, takže flow môže začať posielať niečo nové ešte predtým, než
sa aplikácia nasadí nanovo. Všetko ostatné je prísne: pole, ktoré tam je, musí mať správny
tvar.

| Pole | Typ | Povinné | Čo znamená |
|---|---|---|---|
| `catalog` | `{"id": číslo, "name": text}` | nie | Vintedská kategória. `id` je aspoň 1, `name` najviac 200 znakov. |
| `brand` | `{"id": číslo, "name": text}` | nie | Značka, ten istý tvar. |
| `sizes` | zoznam `{"id", "name"}`, najviac 10 | nie | Veľkosti v rámci tej kategórie. Zmysel majú len spolu s `catalog` — pozri nižšie. |
| `price_to` | číslo, 0 alebo viac | nie | Cenový strop, v mene `currency`. `price_from` neexistuje: aplikácia hľadá zdola nahor. |
| `currency` | text, najviac 3 znaky | nie | `EUR`, `CZK`, … V čom je `price_to` počítané. |
| `search_text` | text, najviac 200 znakov | nie | Voľné slová podané Vintedovmu vlastnému vyhľadávaciemu poľu, navrch k filtrom. |
| `keywords` | zoznam textov, najviac 10 | nie | Slová, na ktorých v názve záleží. Sweep podľa nich zoraďuje výsledky; nikdy podľa nich nefiltruje. |
| `visual_signature` | text, najviac 1000 znakov | nie | Krátky popis toho, ako kus *vyzerá*, na porovnanie s fotkami. |
| `watch_hints` | `{"required_keywords": zoznam textov (najviac 10), "title_pattern": text alebo null}` | nie | Pravidlá pre názov, odložené na neskôr, keby sa zo sweepu niekedy stalo trvalé striehnutie. Nikdy sa nepoužijú ako filter vyhľadávania. |

### Prečo ide s každým id aj meno

`catalog`, `brand` a `sizes` sú dvojice `{id, name}`, nie holé čísla, a tá redundancia je
zámerná. Pri samotnom čísle sa dá overiť len to, či existuje. Pri čísle *a* mene, ktoré si
model myslel, že to je, sa dajú overiť aj proti sebe — a keď si odporujú, aplikácia vie
povedať, **akú značku vlastne pýtala**, namiesto „značka 90804 je zlá".

A je to aj to, čo overenie vôbec umožňuje. Vinted nemá endpoint „existuje toto id značky";
značky a veľkosti sa dajú hľadať len podľa textu, ktorý by človek napísal. Bez mena niet čo
hľadať, takže id bez mena by sa nikdy nedalo potvrdiť.

### Prejdený príklad

Flow dostane:

```json
{"version": 1, "text": "panska bunda Patagonia Torrentshell M do 60 eur", "tld": "sk"}
```

a odpovie:

```json
{
  "catalog": {"id": 2052, "name": "Jackets & Coats"},
  "brand": {"id": 90804, "name": "Patagonia"},
  "sizes": [{"id": 208, "name": "M"}],
  "price_to": 60,
  "currency": "EUR",
  "search_text": "Patagonia Torrentshell",
  "keywords": ["torrentshell"],
  "visual_signature": "ľahká nepremokavá bunda s kapucňou, jednofarebná, dve vrecká na zips, malé logo na ľavej hrudi, bez zateplenia a prešívania",
  "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": null}
}
```

Aplikácia potvrdí všetky štyri id a odpovie volajúcemu:

```json
{
  "params": {
    "catalog_ids": "2052",
    "brand_ids": "90804",
    "size_ids": "208",
    "price_to": "60",
    "currency": "EUR",
    "search_text": "Patagonia Torrentshell",
    "order": "newest_first"
  },
  "tld": "sk",
  "keywords": ["torrentshell"],
  "visual_signature": "ľahká nepremokavá bunda s kapucňou, …",
  "watch_hints": {"required_keywords": ["torrentshell"], "title_pattern": null},
  "labels": {"catalog": "Jackets & Coats", "brand": "Patagonia", "sizes": ["M"]}
}
```

`params` je samotné vyhľadávanie — presne ten slovník, aký uložené vyhľadávanie drží a aký
request na Vinted berie. `labels` nesú mená späť, aby potvrdzovacia obrazovka mohla povedať
„Jackets & Coats / Patagonia / M" namiesto troch čísel, a `keywords`, `visual_signature` a
`watch_hints` sedia *vedľa* `params`, nikdy vnútri: popisujú, ako výsledky zoradiť a čítať,
a vložiť ich do vyhľadávania by zúžilo sweep, ktorý má vidieť všetko.

## Čo bude odmietnuté

Id sa overujú v troch krokoch, od najlacnejšieho, a prvé zlyhanie zastaví zvyšok. Čokoľvek
odmietnuté sa vráti ako `422` s `{"error": "<veta nižšie>"}`.

1. **Kategória**, proti stromu vintedských kategórií, ktorý si aplikácia drží týždeň. Takmer
   vždy nula requestov na Vinted a chytí to, čo si model vymýšľa najčastejšie. Id, ktoré
   nikto nemá:

   ```
   Vinted has no category 9999 ('Jackets & Coats')
   ```

2. **Značka**, hľadaná podľa mena, ktoré flow poslal, v rámci potvrdenej kategórie. Zoznam
   značiek kategórie obsahuje len značky, na ktoré tam práve niečo visí, takže prázdna
   odpoveď sa najprv skúsi znova naprieč celou stránkou, kým sa niečo označí za zlé. Keď to
   zlé je, prídu aj blízke trafy:

   ```
   Vinted has no brand 90804 ('Patagonya') — searching for 'Patagonya' found: Patagonia, Patagucci
   ```

3. **Veľkosti**, proti zoznamu veľkostí tej kategórie. „M" je iné id pre bundu než pre topánku,
   takže veľkosť bez kategórie sa nedá overiť vôbec a radšej sa odmietne, než by prešla ďalej:

   ```
   category 2052 has no size 999 ('XM')
   sizes can only be checked inside a category, and this search has none: 208 ('M')
   ```

Možné sú aj dve iné odpovede a znamenajú niečo iné. `422` pokrýva aj zlyhanie samotného
flowu — neodpovedal načas, je nedostupný, odpovedal niečím, čo nie je JSON, alebo tvarom,
ktorý aplikácia nevie použiť („the mapper answered with an unusable shape: catalog.id: Input
should be greater than or equal to 1"). `502` znamená, že sa nedalo dostať na samotný Vinted,
takže id sa nedali overiť; veta končí „the ids in this search could not be checked". Na to
prvé pomôže preformulovanie, na to druhé už len čakanie.

Odmietnutie je bezpečné a nestojí nič nad rámec toho jedného volania, ktoré ho vyvolalo. Nič
sa nehľadalo, nič sa neuložilo a tá istá veta — alebo lepšia — sa dá poslať hneď znova.

## Ako písať prompt

Prompt je vo tvojom flowe, takže toto je rada, nie zmluva. Je to to, čo potreboval existujúci
enrichment flow:

- **Pomenuj výstupný jazyk výslovne.** Nepredpokladaj, že model odpovie v jazyku otázky.
  Slovenčina a čeština sú si dosť blízke na to, aby medzi nimi model uprostred vety
  preplával, takže povedz, ktorú chceš, *a* daj dve-tri ukážkové slová v nej. Najviac na tom
  záleží pri `visual_signature`, lebo to je text, ktorý číta človek.
- **Pýtaj si id, ktorými si je model istý, inak `null`.** Chýbajúca značka stojí širšie
  vyhľadávanie; vymyslená stojí odmietnutie a druhý pokus. Napíš to do promptu — model bude
  radšej hádať, než nechať pole prázdne, kým mu nepovieš opak.
- **Daj modelu vedieť, pre ktorú stránku mapuje.** `tld` je v každom requeste. Id sa medzi
  krajinami líšia a id kategórie požičané z inej stránky neprejde prvým overením.
- **`search_text` je pomôcka, nie filter.** Vinted ho berie voľne a vráti aj veci, ktoré tie
  slová v názve vôbec nemajú. Čo naozaj musí byť v názve, patrí do `keywords` (na zoraďovanie)
  alebo do `watch_hints.required_keywords` (pre neskoršie trvalé striehnutie).
- **`visual_signature` nechaj pri tom, ako vec vyzerá.** Tvar, farba, zapínanie, vrecká,
  umiestnenie loga — veci viditeľné na fotke. Nie história značky, nie cena, nie kto to nosí.
  Porovnáva sa s náhľadmi fotiek, takže čokoľvek nevyfotiteľné je šum.

## Čo to stojí

Jedno volanie flowu na vyhľadávanie a jedno volanie jazykového modelu v ňom. Preto aplikácia
neúspešné mapovanie nikdy neopakuje: tichý druhý pokus by zdvojnásobil čakanie aj účet za
takmer isto to isté zlyhanie. Neúspešné mapovanie sa vráti rovno aj s dôvodom a stlačiť
tlačidlo znova je tvoje rozhodnutie, nie rozhodnutie aplikácie.
