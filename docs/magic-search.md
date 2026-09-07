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

# The photo check and the full opinion

The mapper above turns a sentence into a search. These two flows are what happens next, and
they exist because of one number: in the reference sweep, of 89 listings that really were
the jacket somebody asked for, **not one named the model in its title**. Sellers write
"Kurtka Patagonia", "Patagonia bunda", "Patagonia jacket M". A ranking built on titles can
only ever rank the words a seller happened to type.

So a sweep run with `--judge` has three stages:

1. **The funnel** — free. Banned words, budget, condition, seller. Titles rank here; they
   never eliminate.
2. **The photo check** (`MAGIC_TRIAGE_WEBHOOK_URL`) — cheap. Every survivor's *thumbnail*
   goes out in batches, and the answer re-orders the list so a listing the photos recognised
   outranks a listing whose title matched and whose photo was rejected.
3. **The full opinion** (`MAGIC_VERDICT_WEBHOOK_URL`) — expensive. The best two or three get
   every full-size photo and the same treatment a standing watch's enrichment gives. Point
   this at a copy of the enrichment flow; the contract is that flow's contract.

Both are optional and both are off until their URL is set. They share
`MAGIC_WEBHOOK_TOKEN` and `MAGIC_TIMEOUT_S` with the mapper: one token and one patience
setting across all three flows.

## What the photo check receives

One `POST` per batch to `MAGIC_TRIAGE_WEBHOOK_URL`, with `Authorization: Bearer
<MAGIC_WEBHOOK_TOKEN>` when a token is set:

```json
{
  "version": 1,
  "source": "sweep",
  "target": {
    "keywords": ["patagonia", "torrentshell"],
    "visual_signature": "grey three-layer shell jacket, hood, two chest pockets",
    "labels": {"catalog": "Jackets & Coats", "brand": "Patagonia"}
  },
  "items": [
    {
      "id": 5551234,
      "thumb_url": "https://images1.vinted.net/t/.../310x430/....jpeg?s=...",
      "title": "Bunda Patagonia panska M",
      "price": "48.00",
      "currency": "EUR",
      "brand": "Patagonia",
      "size": "M",
      "condition": "Very good"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `version` | Always `1` today, same as the mapper's. |
| `source` | Always `"sweep"`. It exists so sweep traffic is separable from standing-watch traffic in your n8n execution history. |
| `target` | What the listings are being compared against. `keywords` and `visual_signature` come from the mapper; `labels` are the human names of the ids the search already filtered on, so the model reads "Patagonia" rather than a brand id. |
| `items` | The batch. Every item carries `thumb_url` and no other image key — see costs below. `thumb_url` may be `null`; judge that item on its title and say so in `reason` rather than dropping it. |

## What the photo check must answer

A single JSON object, `200`. `{"output": {…}}` or `{"result": {…}}` also works, unwrapped
one level, exactly as for the mapper.

```json
{
  "results": [
    {
      "id": 5551234,
      "matches_target": true,
      "confidence": 0.86,
      "reason": "Grey three-layer shell with the hood and chest pockets described."
    },
    {
      "id": 5559876,
      "matches_target": false,
      "confidence": 0.71,
      "reason": "A fleece, not a shell jacket."
    }
  ],
  "usage": {"input_tokens": 41200, "output_tokens": 640}
}
```

| Field | Type | Required | What it means |
|---|---|---|---|
| `results` | list of items | yes (may be empty) | One entry per listing you were sent. |
| `results[].id` | number | yes | The `id` from the request, unchanged. An id nobody sent is dropped; an id nobody answered stays un-triaged. Both are logged as `magic.triage_mismatch`. |
| `results[].matches_target` | true/false | yes | Is this the thing described. |
| `results[].confidence` | number, **0 to 1** | yes | How sure. `0.86`, never `86` — a number out of range is refused outright rather than quietly dominating the ranking. |
| `results[].reason` | text | no | One short line, **shown to the person who ran the sweep** — so its language matters. See "Writing the judging prompts". |
| `usage` | `{"input_tokens", "output_tokens", "cost_eur"}` | no | What the batch cost. Every part optional; see costs below. |

An answer omitting a listing is not an error. The app reconciles by id, leaves that listing
un-triaged, and un-triaged sorts *below* a confirmed match but *above* a rejection — so a
flow that drops an id costs that listing its place in the queue without costing it the run.

## What the full opinion receives

One `POST` per listing to `MAGIC_VERDICT_WEBHOOK_URL` — one candidate per request, because
this stage is billed by the photo and a half-failed batch would leave the app unable to say
which listings it had paid for. The item block is exactly what `deliver/webhook.py` already
sends a standing watch's enrichment flow, minus the `enrichment_url` callback:

```json
{
  "version": 1,
  "source": "sweep",
  "target": {
    "keywords": ["patagonia", "torrentshell"],
    "visual_signature": "grey three-layer shell jacket, hood, two chest pockets",
    "labels": {"catalog": "Jackets & Coats", "brand": "Patagonia"}
  },
  "items": [
    {
      "id": 5551234,
      "site": "vinted.sk",
      "title": "Bunda Patagonia panska M",
      "url": "https://www.vinted.sk/items/5551234",
      "brand": "Patagonia",
      "size": "M",
      "condition": "Very good",
      "price": "48.00",
      "total_price": "52.30",
      "currency": "EUR",
      "photo_url": "https://images1.vinted.net/t/.../f800/....jpeg?s=...",
      "photo_urls": ["https://images1.vinted.net/t/.../f800/....jpeg?s=..."],
      "seller": "some_seller"
    }
  ]
}
```

`items` is a one-element list rather than a bare object on purpose: a copy of the enrichment
flow already iterates `items`, so reusing it needs no node rewiring. There is no
`enrichment_url` — a sweep's answer comes back on this response, and the app writes it to
the sweep's own tables. It is never written to the watch's `items` table, which is why a
sweep can look at anything without blinding your standing searches.

## What the full opinion must answer

Either shape works, and the app tells them apart by **type, not by key**: a JSON *object*
under `verdict` means the envelope; anything else means the body itself is the answer.

The envelope, when you want to report cost:

```json
{
  "verdict": {
    "score": 82,
    "model": "Patagonia Torrentshell 3L",
    "retail_price": 180,
    "retail_source": "patagonia.com",
    "matches_query": true,
    "risk": null,
    "verdict": "Genuine, and well under what it usually goes for."
  },
  "usage": {"input_tokens": 9800, "output_tokens": 320, "cost_eur": 0.014}
}
```

An unmodified copy of the enrichment flow answers with the inner object alone, and that is
accepted as-is:

```json
{
  "score": 82,
  "model": "Patagonia Torrentshell 3L",
  "retail_price": 180,
  "retail_source": "patagonia.com",
  "matches_query": true,
  "risk": null,
  "verdict": "Genuine, and well under what it usually goes for."
}
```

Every field is optional — a partial opinion beats none. `score` is 0-100, `retail_price` is
0 or more, `matches_query` says whether it is the product searched for at all, and `verdict`
is the one line a person reads. Those are the same fields, with the same meanings and the
same limits, as [the enrichment callback](configuration.md); this page does not redefine
them, it reuses them.

## What the judging stages cost

Read this before writing either prompt, because **most of the cost model is the app's job,
not the flow's — and asking for it in the prompt is how it breaks.**

- **Thumbnails, not photos.** The photo check is sent `thumb_url` and no other image key. A
  ~310x430 thumbnail is roughly 180 image tokens; a full-size photo is about six times that,
  and a sweep sends around 150 of them. Sending full-size photos to the photo check would
  still work — it would just quietly cost six times as much.
- **The app decides the batch size**, from `SWEEP_TRIAGE_BATCH` (20 by default). Do not ask
  the prompt to "process in batches"; it receives exactly one batch per call and its only
  job is to answer for the items in front of it.
- **The app decides how many full opinions get bought**, from `SWEEP_MAX_VERDICTS` (3 by
  default). The list is cut to that length *before a single request is made*, so "at most
  three" is a property of a list rather than a sentence a model may ignore. Do not ask the
  prompt to "pick the best three" — by the time it is called, that choice is already made.
- **`usage` is optional and the arithmetic is the app's.** Report it and the app uses your
  figures; report `cost_eur` and that wins outright. Report nothing and the app estimates
  from `MAGIC_COST_PER_MTOK_IN` and `MAGIC_COST_PER_MTOK_OUT`. Either way the run ends with
  one `sweep.cost` line naming listings funnelled, photos checked, opinions bought, tokens
  and euros — and `vinted-sniper sweep <url> --judge` prints the same line for you.
- **Neither stage is retried.** A failed batch closes the run as `partial` and skips the
  expensive stage; a failed single opinion is skipped and the rest are still bought. Nothing
  already paid for is discarded, and the bill for it is still reported.

## Writing the judging prompts

The prompts live in your flows. This is what the existing flows needed:

- **Name the output language explicitly, and treat `sk` and `cs` as different languages.**
  `reason` and `verdict` are shown to the person who ran the sweep. Slovak and Czech are
  close enough that a model drifts between them mid-sentence, so say which one you want
  *and* give worked word examples: Slovak "bunda", "veľkosť", "stav", "pravdepodobne" —
  Czech would be "bunda", "velikost", "stav", "pravděpodobně". Without the examples the
  answer comes back in a plausible blend of the two.
- **Say what the reason is for.** One short line explaining what in the photo decided it —
  "grey shell, hood, two chest pockets" — not a restatement of the title.
- **Confidence is 0 to 1.** Say so in the prompt and show an example. A flow answering `86`
  for `0.86` is refused, which is deliberate: silently accepting it would let one listing
  dominate every real match.
- **Judge the photo, not the words.** The whole reason this stage exists is that titles lie
  by omission. A listing whose title says nothing and whose photo is obviously the thing
  should come back `matches_target: true`.
- **Keep it plain.** No percentages of a distribution, no "median price", no statistics
  vocabulary — this text is read by somebody deciding whether to click a listing.

## When a judging flow goes wrong

- **A re-imported flow is silently switched off.** n8n's `import:workflow` deactivates the
  workflow it imports. The URL still exists, the app still posts to it, and nothing answers.
  After importing or re-importing either flow, open it and publish it again.
- **The token must not contain the word "Bearer".** The app sends `Authorization: Bearer
  <token>`. If `MAGIC_WEBHOOK_TOKEN` is itself set to `Bearer abc123`, the header reads
  `Bearer Bearer abc123` and the flow answers `401` — which looks exactly like a permissions
  problem and is not one. Store the token alone.
- **`the photo check answered 404`** usually means the flow is in test mode: n8n's test
  webhook URL only answers while you are watching the canvas. Use the production URL.
- **A sweep that ranks nothing** with `--judge` set: check the `magic.triage_mismatch` log
  line. A flow answering with ids it invented, or with none of the ids it was sent, leaves
  every listing un-triaged and the ranking falls back to titles.
- **A sweep that costs nothing** means no `usage` came back and both `MAGIC_COST_PER_MTOK_*`
  rates are 0. That is a configuration answer, not a free lunch.


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

---

# Kontrola fotiek a plný posudok

Mapper vyššie zmení vetu na vyhľadávanie. Tieto dva flowy sú to, čo príde potom, a existujú
kvôli jednému číslu: v referenčnom sweepe z 89 inzerátov, ktoré naozaj boli tá bunda, čo
niekto hľadal, **ani jeden nemal model v názve**. Predajcovia píšu „Kurtka Patagonia",
„Patagonia bunda", „Patagonia jacket M". Zoraďovanie postavené na názvoch vie zoradiť len
slová, ktoré predajca náhodou napísal.

Sweep spustený s `--judge` má preto tri fázy:

1. **Lievik** — zadarmo. Zakázané slová, rozpočet, stav, predajca. Názvy tu zoraďujú; nikdy
   nevyraďujú.
2. **Kontrola fotiek** (`MAGIC_TRIAGE_WEBHOOK_URL`) — lacná. Každý, kto prežil lievik, ide
   von v dávkach ako *náhľad fotky*, a odpoveď preusporiada zoznam tak, že inzerát, ktorý
   fotka spoznala, predbehne inzerát, ktorého názov sedel a ktorého fotku model odmietol.
3. **Plný posudok** (`MAGIC_VERDICT_WEBHOOK_URL`) — drahý. Najlepšie dva-tri dostanú všetky
   fotky v plnej veľkosti a to isté zaobchádzanie, aké dáva enrichment trvalému striehnutiu.
   Nasmeruj to na kópiu enrichment flowu; jeho zmluva je zmluvou tohto flowu.

Oba sú voliteľné a oba sú vypnuté, kým nie je nastavená ich URL. Zdieľajú
`MAGIC_WEBHOOK_TOKEN` a `MAGIC_TIMEOUT_S` s mapperom: jeden token a jedno nastavenie
trpezlivosti naprieč všetkými tromi flowmi.

## Čo kontrola fotiek dostane

Jeden `POST` na dávku na `MAGIC_TRIAGE_WEBHOOK_URL`, s hlavičkou `Authorization: Bearer
<MAGIC_WEBHOOK_TOKEN>`, keď je token nastavený:

```json
{
  "version": 1,
  "source": "sweep",
  "target": {
    "keywords": ["patagonia", "torrentshell"],
    "visual_signature": "sivá trojvrstvová šuštiaková bunda, kapucňa, dve náprsné vrecká",
    "labels": {"catalog": "Bundy a kabáty", "brand": "Patagonia"}
  },
  "items": [
    {
      "id": 5551234,
      "thumb_url": "https://images1.vinted.net/t/.../310x430/....jpeg?s=...",
      "title": "Bunda Patagonia panska M",
      "price": "48.00",
      "currency": "EUR",
      "brand": "Patagonia",
      "size": "M",
      "condition": "Veľmi dobrý"
    }
  ]
}
```

| Pole | Význam |
|---|---|
| `version` | Dnes vždy `1`, rovnako ako u mappera. |
| `source` | Vždy `"sweep"`. Existuje preto, aby sa prevádzka zo sweepu dala v histórii behov n8n odlíšiť od prevádzky trvalého striehnutia. |
| `target` | Voči čomu sa inzeráty porovnávajú. `keywords` a `visual_signature` prichádzajú z mappera; `labels` sú ľudské mená id, na ktoré už vyhľadávanie filtrovalo, takže model číta „Patagonia" a nie id značky. |
| `items` | Dávka. Každá položka nesie `thumb_url` a žiadny iný obrázkový kľúč — pozri náklady nižšie. `thumb_url` môže byť `null`; taký inzerát posúď podľa názvu a napíš to do `reason`, namiesto toho, aby si ho vynechal. |

## Čo musí kontrola fotiek odpovedať

Jeden JSON objekt, `200`. Funguje aj `{"output": {…}}` alebo `{"result": {…}}`, rozbalené o
jednu úroveň, presne ako u mappera.

```json
{
  "results": [
    {
      "id": 5551234,
      "matches_target": true,
      "confidence": 0.86,
      "reason": "Sivá trojvrstvová šuštiaková bunda s kapucňou a náprsnými vreckami."
    },
    {
      "id": 5559876,
      "matches_target": false,
      "confidence": 0.71,
      "reason": "Fleecová mikina, nie šuštiaková bunda."
    }
  ],
  "usage": {"input_tokens": 41200, "output_tokens": 640}
}
```

| Pole | Typ | Povinné | Čo znamená |
|---|---|---|---|
| `results` | zoznam položiek | áno (môže byť prázdny) | Jeden záznam na každý inzerát, ktorý si dostal. |
| `results[].id` | číslo | áno | `id` z requestu, nezmenené. Id, ktoré nikto neposlal, sa zahodí; id, na ktoré nikto neodpovedal, zostane neposúdené. Oboje sa zaloguje ako `magic.triage_mismatch`. |
| `results[].matches_target` | true/false | áno | Je to tá vec, ktorá je popísaná. |
| `results[].confidence` | číslo, **0 až 1** | áno | Ako veľmi si si istý. `0.86`, nikdy nie `86` — číslo mimo rozsahu sa rovno odmietne, namiesto toho, aby ticho ovládlo celé poradie. |
| `results[].reason` | text | nie | Jeden krátky riadok, **ktorý sa ukáže človeku, čo sweep spustil** — takže na jeho jazyku záleží. Pozri „Ako písať prompty na posudzovanie". |
| `usage` | `{"input_tokens", "output_tokens", "cost_eur"}` | nie | Čo dávka stála. Každá časť je voliteľná; pozri náklady nižšie. |

Odpoveď, ktorá nejaký inzerát vynechá, nie je chyba. Aplikácia si to spáruje podľa id, ten
inzerát nechá neposúdený, a neposúdený sa radí *pod* potvrdenú zhodu, ale *nad* odmietnutie
— takže flow, ktorý id vynechá, stojí ten inzerát miesto v poradí, ale nie účasť v behu.

## Čo plný posudok dostane

Jeden `POST` na inzerát na `MAGIC_VERDICT_WEBHOOK_URL` — jeden kandidát na request, lebo
táto fáza sa platí za fotku a napoly zlyhaná dávka by aplikáciu nechala bez odpovede na to,
za ktoré inzeráty vlastne zaplatila. Blok položky je presne to, čo `deliver/webhook.py` už
teraz posiela enrichment flowu trvalého striehnutia, bez callbacku `enrichment_url`:

```json
{
  "version": 1,
  "source": "sweep",
  "target": {
    "keywords": ["patagonia", "torrentshell"],
    "visual_signature": "sivá trojvrstvová šuštiaková bunda, kapucňa, dve náprsné vrecká",
    "labels": {"catalog": "Bundy a kabáty", "brand": "Patagonia"}
  },
  "items": [
    {
      "id": 5551234,
      "site": "vinted.sk",
      "title": "Bunda Patagonia panska M",
      "url": "https://www.vinted.sk/items/5551234",
      "brand": "Patagonia",
      "size": "M",
      "condition": "Veľmi dobrý",
      "price": "48.00",
      "total_price": "52.30",
      "currency": "EUR",
      "photo_url": "https://images1.vinted.net/t/.../f800/....jpeg?s=...",
      "photo_urls": ["https://images1.vinted.net/t/.../f800/....jpeg?s=..."],
      "seller": "some_seller"
    }
  ]
}
```

`items` je zámerne jednoprvkový zoznam, nie holý objekt: kópia enrichment flowu už teraz
prechádza `items`, takže na jej znovupoužitie netreba prepájať žiadne nody. `enrichment_url`
tam nie je — odpoveď sweepu príde v tejto response a aplikácia ju zapíše do vlastných tabuliek
sweepu. Nikdy sa nezapíše do tabuľky `items`, ktorú používa striehnutie, a práve preto sa
sweep môže pozerať na čokoľvek bez toho, aby oslepil tvoje trvalé vyhľadávania.

## Čo musí plný posudok odpovedať

Funguje ktorýkoľvek z dvoch tvarov a aplikácia ich rozlíši **podľa typu, nie podľa kľúča**:
JSON *objekt* pod `verdict` znamená obálku; čokoľvek iné znamená, že odpoveďou je celé telo.

Obálka, keď chceš hlásiť aj cenu:

```json
{
  "verdict": {
    "score": 82,
    "model": "Patagonia Torrentshell 3L",
    "retail_price": 180,
    "retail_source": "patagonia.com",
    "matches_query": true,
    "risk": null,
    "verdict": "Pravé a výrazne pod tým, za čo sa to bežne predáva."
  },
  "usage": {"input_tokens": 9800, "output_tokens": 320, "cost_eur": 0.014}
}
```

Nezmenená kópia enrichment flowu odpovie samotným vnútorným objektom a aj to sa berie tak,
ako je:

```json
{
  "score": 82,
  "model": "Patagonia Torrentshell 3L",
  "retail_price": 180,
  "retail_source": "patagonia.com",
  "matches_query": true,
  "risk": null,
  "verdict": "Pravé a výrazne pod tým, za čo sa to bežne predáva."
}
```

Každé pole je voliteľné — čiastočný posudok je lepší než žiadny. `score` je 0-100,
`retail_price` je 0 alebo viac, `matches_query` hovorí, či to vôbec je ten produkt, ktorý sa
hľadal, a `verdict` je ten jeden riadok, ktorý si prečíta človek. Sú to tie isté polia, s tým
istým významom a tými istými limitmi, ako má [enrichment callback](configuration.md); táto
stránka ich nedefinuje nanovo, znovu ich používa.

## Čo posudzovacie fázy stoja

Prečítaj si to skôr, než napíšeš ktorýkoľvek z tých dvoch promptov, lebo **väčšina cenového
modelu je práca aplikácie, nie flowu — a pýtať si ju v prompte je presne to, čím sa to
pokazí.**

- **Náhľady, nie fotky.** Kontrola fotiek dostáva `thumb_url` a žiadny iný obrázkový kľúč.
  Náhľad ~310x430 je zhruba 180 obrázkových tokenov; fotka v plnej veľkosti je asi šesťkrát
  toľko a sweep ich posiela okolo 150. Poslať kontrole fotiek plné veľkosti by fungovalo —
  len by to ticho stálo šesťkrát viac.
- **O veľkosti dávky rozhoduje aplikácia**, cez `SWEEP_TRIAGE_BATCH` (predvolene 20). Nepýtaj
  si v prompte „spracuj to po dávkach"; flow dostane presne jednu dávku na volanie a jeho
  jediná úloha je odpovedať na položky, ktoré má pred sebou.
- **O tom, koľko plných posudkov sa kúpi, rozhoduje aplikácia**, cez `SWEEP_MAX_VERDICTS`
  (predvolene 3). Zoznam sa skráti na túto dĺžku *skôr, než vznikne jediný request*, takže
  „najviac tri" je vlastnosť zoznamu, a nie veta, ktorú model môže ignorovať. Nepýtaj si v
  prompte „vyber tie najlepšie tri" — keď sa flow volá, to je už rozhodnuté.
- **`usage` je voliteľné a tá aritmetika je práca aplikácie.** Keď ho pošleš, aplikácia
  použije tvoje čísla; keď pošleš `cost_eur`, vyhráva rovno ono. Keď nepošleš nič, aplikácia
  odhadne cenu z `MAGIC_COST_PER_MTOK_IN` a `MAGIC_COST_PER_MTOK_OUT`. Tak či tak beh skončí
  jedným riadkom `sweep.cost`, ktorý povie, koľko inzerátov prešlo lievikom, koľko fotiek sa
  skontrolovalo, koľko posudkov sa kúpilo, koľko tokenov a koľko eur — a
  `vinted-sniper sweep <url> --judge` ti ten istý riadok vypíše.
- **Ani jedna fáza sa neopakuje.** Zlyhaná dávka zavrie beh ako `partial` a preskočí tú drahú
  fázu; zlyhaný jeden posudok sa preskočí a ostatné sa aj tak kúpia. Nič, čo už bolo
  zaplatené, sa nezahadzuje — a účet za to sa aj tak nahlási.

## Ako písať prompty na posudzovanie

Prompty sú v tvojich flowoch. Toto je to, čo potrebovali tie existujúce:

- **Pomenuj výstupný jazyk výslovne a ber `sk` a `cs` ako dva rôzne jazyky.** `reason` a
  `verdict` sa ukazujú človeku, ktorý sweep spustil. Slovenčina a čeština sú si dosť blízke
  na to, aby model medzi nimi uprostred vety preplával, takže povedz, ktorú chceš, *a* daj
  ukážkové slová: slovensky „bunda", „veľkosť", „stav", „pravdepodobne" — česky by to bolo
  „bunda", „velikost", „stav", „pravděpodobně". Bez tých príkladov príde odpoveď v
  hodnovernej zmesi oboch.
- **Povedz, načo `reason` je.** Jeden krátky riadok o tom, čo na fotke rozhodlo — „sivá
  šuštiakovka, kapucňa, dve náprsné vrecká" — nie prerozprávanie názvu.
- **Confidence je 0 až 1.** Napíš to do promptu a ukáž príklad. Flow, ktorý odpovie `86`
  namiesto `0.86`, bude odmietnutý, a je to zámer: ticho to prijať by znamenalo, že jeden
  inzerát ovládne všetky skutočné zhody.
- **Posudzuj fotku, nie slová.** Celý dôvod, prečo táto fáza existuje, je, že názvy klamú
  tým, čo zamlčia. Inzerát, ktorého názov nehovorí nič a ktorého fotka je zjavne tá vec, sa
  má vrátiť ako `matches_target: true`.
- **Píš to normálne.** Žiadne percentily, žiadna „mediánová cena", žiadny štatistický
  slovník — tento text číta človek, ktorý sa rozhoduje, či na inzerát klikne.

## Keď posudzovací flow zlyháva

- **Znovu naimportovaný flow je ticho vypnutý.** n8n príkazom `import:workflow` deaktivuje
  workflow, ktorý importuje. URL stále existuje, aplikácia naň stále posiela requesty a nič
  neodpovedá. Po importe alebo reimporte ktoréhokoľvek z tých flowov ho otvor a znovu
  publikuj.
- **Token nesmie obsahovať slovo „Bearer".** Aplikácia posiela `Authorization: Bearer
  <token>`. Keď je `MAGIC_WEBHOOK_TOKEN` nastavený na `Bearer abc123`, hlavička vyjde ako
  `Bearer Bearer abc123` a flow odpovie `401` — čo vyzerá presne ako problém s oprávneniami,
  a nie je ním. Ulož samotný token.
- **`the photo check answered 404`** obvykle znamená, že flow je v testovacom režime: testovacia
  webhook URL v n8n odpovedá len vtedy, keď sa pozeráš na plátno. Použi produkčnú URL.
- **Sweep, ktorý s `--judge` nič nezoradí:** pozri sa na logovací riadok
  `magic.triage_mismatch`. Flow, ktorý odpovie vymyslenými id alebo žiadnym z tých, ktoré
  dostal, nechá všetky inzeráty neposúdené a poradie spadne späť na názvy.
- **Sweep, ktorý nič nestál,** znamená, že neprišlo žiadne `usage` a obe sadzby
  `MAGIC_COST_PER_MTOK_*` sú 0. To je odpoveď o konfigurácii, nie obed zadarmo.
