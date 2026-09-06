# Enrichment: letting an agent judge a listing before you see it

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
   restart. Set `VINTED_SNIPER_WEB_AUTH_TOKEN` if it is not already — the callback needs it.
3. Have the agent `POST` its verdict to `enrichment_url` with
   `Authorization: Bearer <WEB_AUTH_TOKEN>`.

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

**Telegram.** A top line: `🔥 HOT DEAL · deal 87/100 · retail ~160 EUR · -60%` when the score
is at least `ENRICHMENT_HIGHLIGHT_SCORE`; `🤖 …` otherwise; `💤 …` and no notification sound
when the score is below `ENRICHMENT_SILENT_BELOW` or `matches_query` is false. Then the
listing as usual, then "Looks like: …" and the verdict in italics.

**Discord.** A "🤖 Verdict" field on the embed.

**Dashboard.** A score badge on the listing card, the verdict as its tooltip.

## Scoring guidance for the agent

The score is yours to define, but the one the reference workflow uses weighs, in order:
discount against retail (total price, buyer protection included); whether it is the product
searched for at all; condition; seller history; and authenticity risk, which caps the score
rather than merely lowering it — a suspiciously cheap "genuine" item is a 30, not an 85.
A reverse image search is rarely needed: brand, title and one clear photo identify most
products, and a text search for the retail price is a fraction of the cost.
