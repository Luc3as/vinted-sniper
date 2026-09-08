# Enrichment: letting an agent judge a listing before you see it

*[Slovenská verzia nižšie ↓](#slovensky)*

vinted-sniper knows what the catalog says — title, price, photos, seller. It does not know
whether the jacket in the photos is the model you searched for, what it costs new, or
whether the price is a bargain or a warning sign. Those are questions for something that
can look at pictures and search the web. This page describes the loop that lets you plug
one in — an n8n workflow with an LLM agent is the reference setup — without moving
delivery out of vinted-sniper: the alert still arrives in Telegram with its buttons, quiet
hours and digests, just with a verdict woven in.

## How the loop works

```
poller finds a listing
  ├─ webhook destination      → fires at once, payload carries "enrichment_url"
  └─ chat destinations        → held for ENRICHMENT_WAIT_S seconds

agent looks at photos, identifies the product, checks the retail price, scores the deal
  └─ POST enrichment_url      → stored on the listing; the held alert is released now

nothing posted in time?       → the alert goes out as it always did
```

Silence from the agent costs a delay, never an alert. If the verdict arrives after the
alert went out, it is stored (the dashboard shows it) and, when it scores at least
`ENRICHMENT_HIGHLIGHT_SCORE` and does not say the listing is the wrong product, a short
"verdict is in: hot deal" follow-up goes to the same chat destinations. A late verdict
that says "nothing special" is kept quiet: it would not earn a second message.

## Setting it up

1. Add a webhook destination pointing at your agent (n8n: a Webhook node's production URL).
   Route the searches you want judged to it, alongside your Telegram destination.
2. Set `VINTED_SNIPER_ENRICHMENT_WAIT_S=90` (or however long your agent usually takes) and
   restart. Set `VINTED_SNIPER_CALLBACK_AUTH_TOKEN` — a token that opens only this
   callback, so the agent never holds the dashboard password.
3. Have the agent `POST` its verdict to `enrichment_url` with
   `Authorization: Bearer <CALLBACK_AUTH_TOKEN>`. A flow still sending
   `WEB_AUTH_TOKEN` keeps working, so you can switch tokens without downtime.

## What the agent receives

The webhook payload from [configuration.md](configuration.md), with these fields the agent
cares about:

| Field | Meaning |
|---|---|
| `search`, `search_id` | The search's name and id. The name is usually the text searched for. |
| `items[].title`, `brand`, `size`, `condition` | What the seller wrote. |
| `items[].price`, `total_price`, `currency` | Asking price and what the buyer actually pays. |
| `items[].photo_urls` | Every photo, full size. Two or three are usually enough to identify a product. |
| `items[].seller`, `seller_rating`, `seller_reviews` | Who is selling, 0–1 rating, review count. |
| `items[].enrichment_url` | Where to post the verdict. `null` while the dashboard is off. |
| `items[].favourites`, `views`, `listed_minutes_ago`, `favourites_per_hour` | Demand. A listing twelve minutes old with six hearts is one the market has already noticed. |
| `items[].market` | Where the price sits among everything this search has shown in the last 30 days (`null` until there are ten points): `n`, `p10`, `p25`, `median`, `p75`, `median_same_condition`, `this_percentile` (share of listings cheaper than this one), and `sells_fast_under` — the median price of listings that vanished within a day, the closest thing to a sold price the catalog offers. Built from every listing on the page, filters or not, at no extra requests. |
| `items[].known_retail` | Retail prices earlier verdicts reported for this search, `[{model, price, currency, source}]`. Reuse instead of searching again when the product matches. |
| `items[].reader_language` | `en` or `sk`: the language the buyer reads this search's alerts in (the most common among its chat destinations). Write `verdict` in it. |
| `items[].buyer_feedback` | The buyer's thumbs on recent verdicts for this search: `[{title, condition, total_price, agent_score, agent_model, buyer_said}]`. What this particular buyer calls a deal. |

## What the agent posts back

`POST {enrichment_url}` with a JSON body. Every field is optional; a partial verdict is
better than none.

```json
{
  "score": 87,
  "model": "Patagonia Torrentshell 3L Jacket (men's, 2022)",
  "retail_price": 160,
  "retail_source": "patagonia.com",
  "matches_query": true,
  "risk": null,
  "verdict": "Genuine 3L model, current season, ~60% under retail for 'very good' condition."
}
```

| Field | Type | Meaning |
|---|---|---|
| `score` | 0–100 | How good a deal this is, all things considered. |
| `model` | text | What product this actually is. |
| `retail_price` | number | What it costs new, in the listing's currency. |
| `retail_source` | text | Where that price came from. |
| `matches_query` | bool | Is it what the search was after, or a lookalike that mentions it? |
| `risk` | text | Authenticity worries: stock photos, missing labels, a price far too low, a seller with no history. |
| `verdict` | text | One sentence for a human. |

Responses: `200 {"ok": true}`, `401` for a missing or wrong token, `404` for a listing that
is no longer stored (pruned after `ITEM_RETENTION_DAYS`), `422` for a body that does not
fit.

## How the verdict shows

**Telegram.** The listing title leads, then the verdict block:
`🔥 HOT DEAL · deal 87/100 · retail ~160 EUR · -60%` when the score is at least
`ENRICHMENT_HIGHLIGHT_SCORE`; `🤖 …` otherwise; `💤 …` and no notification sound when the
score is below `ENRICHMENT_SILENT_BELOW` or `matches_query` is false — followed by the
verdict sentence and "Looks like: …" in italics, then the price and the other facts.

**Discord.** A "🤖 Verdict" field on the embed.

**Dashboard.** A score badge on the listing card, the verdict as its tooltip.

## Scoring guidance for the agent

Retail is one leg of three. What a thing costs new says little about what it sells for
used; `market` says that, and `sells_fast_under` says what people actually pay. The
reference workflow scores, in order: the listing's percentile in its market (a listing
under `p10` in good condition from a trusted seller is a bargain; under `p10` from a seller
with no history and stock photos is a scam, not a discount); demand (`favourites_per_hour`);
discount against retail; condition and seller history; and authenticity risk, which caps
the score rather than merely lowering it. `buyer_feedback` tunes all of that to one
person's taste. A reverse image search is rarely needed: brand, title and one clear photo
identify most products.

**Verdicts feed back.** A verdict's `model` and `retail_price` are cached per search and
sent as `known_retail` next time. The 👍/👎 buttons under a Telegram alert become
`buyer_feedback`.

---

<a name="slovensky"></a>

# Enrichment: nechaj agenta posúdiť inzerát skôr, než ho uvidíš (slovensky)

vinted-sniper vie, čo hovorí katalóg — názov, cenu, fotky, predajcu. Nevie, či bunda na
fotkách je model, ktorý si hľadal, koľko stojí nová, ani či je cena výhra alebo varovný
signál. To sú otázky pre niečo, čo sa vie pozrieť na obrázky a hľadať na webe. Táto stránka
popisuje slučku, cez ktorú si také niečo zapojíš — referenčné riešenie je n8n workflow s LLM
agentom — bez toho, aby doručovanie opustilo vinted-sniper: alert stále príde do Telegramu so
svojimi tlačidlami, quiet hours a digestami, len s votkaným verdiktom.

## Ako slučka funguje

```
poller nájde inzerát
  ├─ webhook cieľ             → vystrelí hneď, payload nesie "enrichment_url"
  └─ chatové ciele            → podržané ENRICHMENT_WAIT_S sekúnd

agent si pozrie fotky, identifikuje produkt, overí retail cenu, oskóruje deal
  └─ POST enrichment_url      → uloží sa k inzerátu; podržaný alert sa hneď uvoľní

nič neprišlo načas?           → alert odíde tak, ako vždy
```

Ticho od agenta stojí zdržanie, nikdy nie alert. Ak verdikt príde po odoslaní alertu, uloží
sa (dashboard ho ukáže) a — keď má skóre aspoň `ENRICHMENT_HIGHLIGHT_SCORE` a nehovorí, že
ide o nesprávny produkt — do tých istých chatových cieľov odíde krátky follow-up „verdikt je
tu: hot deal". Oneskorený verdikt v zmysle „nič extra" zostane ticho: druhú správu by si
nezaslúžil.

## Nastavenie

1. Pridaj webhook cieľ mieriaci na tvojho agenta (n8n: produkčná URL Webhook nodu). Nasmeruj
   naň vyhľadávania, ktoré chceš posudzovať, popri tvojom Telegram cieli.
2. Nastav `VINTED_SNIPER_ENRICHMENT_WAIT_S=90` (alebo koľko tvoj agent obvykle potrebuje) a
   reštartuj. Nastav `VINTED_SNIPER_CALLBACK_AUTH_TOKEN` — token, ktorý otvára iba tento
   callback, takže agent nikdy nedrží heslo k dashboardu.
3. Agent nech `POST`-ne verdikt na `enrichment_url` s hlavičkou
   `Authorization: Bearer <CALLBACK_AUTH_TOKEN>`. Flow, ktorý stále posiela
   `WEB_AUTH_TOKEN`, funguje ďalej, takže tokeny vymeníš bez výpadku.

## Čo agent dostane

Webhook payload z [configuration.md](configuration.md), s týmito poľami, na ktorých agentovi
záleží:

| Pole | Význam |
|---|---|
| `search`, `search_id` | Meno a id vyhľadávania. Meno je obvykle hľadaný text. |
| `items[].title`, `brand`, `size`, `condition` | Čo napísal predajca. |
| `items[].price`, `total_price`, `currency` | Pýtaná cena a čo kupujúci naozaj zaplatí. |
| `items[].photo_urls` | Všetky fotky v plnej veľkosti. Na identifikáciu produktu obvykle stačia dve-tri. |
| `items[].seller`, `seller_rating`, `seller_reviews` | Kto predáva, hodnotenie 0–1, počet recenzií. |
| `items[].enrichment_url` | Kam poslať verdikt. `null`, kým je dashboard vypnutý. |
| `items[].favourites`, `views`, `listed_minutes_ago`, `favourites_per_hour` | Dopyt. Inzerát starý dvanásť minút so šiestimi srdiečkami si trh už všimol. |
| `items[].market` | Kde cena sedí medzi všetkým, čo toto vyhľadávanie ukázalo za posledných 30 dní (`null`, kým nie je desať bodov): `n`, `p10`, `p25`, `median`, `p75`, `median_same_condition`, `this_percentile` (podiel lacnejších inzerátov) a `sells_fast_under` — medián cien inzerátov, ktoré zmizli do dňa; najbližšia vec k predajnej cene, akú katalóg ponúka. Stavané z každého inzerátu na stránke, bez requestov navyše. |
| `items[].known_retail` | Retail ceny, ktoré predchádzajúce verdikty nahlásili pre toto vyhľadávanie, `[{model, price, currency, source}]`. Pri zhode produktu použi znova namiesto nového hľadania. |
| `items[].reader_language` | `en` alebo `sk`: jazyk, v ktorom kupujúci číta alerty tohto vyhľadávania (najčastejší medzi jeho chatovými cieľmi). Píš `verdict` v ňom. |
| `items[].buyer_feedback` | Palce kupujúceho na nedávne verdikty tohto vyhľadávania: `[{title, condition, total_price, agent_score, agent_model, buyer_said}]`. Čo tento konkrétny kupujúci považuje za deal. |

## Čo agent pošle späť

`POST {enrichment_url}` s JSON telom. Každé pole je voliteľné; čiastočný verdikt je lepší
ako žiadny.

```json
{
  "score": 87,
  "model": "Patagonia Torrentshell 3L Jacket (men's, 2022)",
  "retail_price": 160,
  "retail_source": "patagonia.com",
  "matches_query": true,
  "risk": null,
  "verdict": "Genuine 3L model, current season, ~60% under retail for 'very good' condition."
}
```

| Pole | Typ | Význam |
|---|---|---|
| `score` | 0–100 | Aký dobrý deal to je, so všetkým zohľadneným. |
| `model` | text | Aký produkt to v skutočnosti je. |
| `retail_price` | číslo | Koľko stojí nový, v mene inzerátu. |
| `retail_source` | text | Odkiaľ tá cena je. |
| `matches_query` | bool | Je to, čo vyhľadávanie hľadalo, alebo napodobenina, ktorá to len spomína? |
| `risk` | text | Obavy o pravosť: stock fotky, chýbajúce štítky, podozrivo nízka cena, predajca bez histórie. |
| `verdict` | text | Jedna veta pre človeka. |

Odpovede: `200 {"ok": true}`, `401` pri chýbajúcom alebo zlom tokene, `404` pre inzerát,
ktorý už nie je uložený (mazané po `ITEM_RETENTION_DAYS`), `422` pre telo, ktoré nesedí.

## Ako sa verdikt zobrazí

**Telegram.** Najprv názov inzerátu, potom blok verdiktu:
`🔥 HOT DEAL · deal 87/100 · retail ~160 EUR · -60%` pri skóre aspoň
`ENRICHMENT_HIGHLIGHT_SCORE`; inak `🤖 …`; `💤 …` bez zvuku notifikácie pri skóre pod
`ENRICHMENT_SILENT_BELOW` alebo pri `matches_query: false` — za ním veta verdiktu a
„Looks like: …" kurzívou, potom cena a ostatné fakty.

**Discord.** Pole „🤖 Verdict" na embede.

**Dashboard.** Odznak so skóre na karte inzerátu, verdikt ako jeho tooltip.

## Ako má agent skórovať

Retail je jedna noha z troch. Čo vec stojí nová, hovorí málo o tom, za čo sa predáva
použitá; to hovorí `market` a `sells_fast_under` hovorí, čo ľudia naozaj platia. Referenčný
workflow skóruje v poradí: percentil inzerátu na jeho trhu (inzerát pod `p10` v dobrom stave
od dôveryhodného predajcu je výhra; pod `p10` od predajcu bez histórie so stock fotkami je
podvod, nie zľava); dopyt (`favourites_per_hour`); zľavu voči retailu; stav a históriu
predajcu; a riziko pravosti, ktoré skóre zastropuje, nie iba zníži. `buyer_feedback` to celé
ladí na vkus jedného človeka. Reverzné hľadanie obrázkov treba málokedy: značka, názov a
jedna ostrá fotka identifikujú väčšinu produktov.

**Verdikty sa vracajú do obehu.** `model` a `retail_price` z verdiktu sa cachujú per
vyhľadávanie a nabudúce odchádzajú ako `known_retail`. Tlačidlá 👍/👎 pod Telegram alertom sa
stávajú `buyer_feedback`.
