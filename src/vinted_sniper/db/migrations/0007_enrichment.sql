-- What an outside brain (an n8n agent, say) concluded about a listing: which product it
-- really is, what it costs new, whether it is what the search was after, how good the
-- deal is. Filled in through POST /api/items/{id}/enrichment; NULL until then.
ALTER TABLE items ADD COLUMN enrich_score         INTEGER;
ALTER TABLE items ADD COLUMN enrich_model         TEXT;
ALTER TABLE items ADD COLUMN enrich_retail_price  REAL;
ALTER TABLE items ADD COLUMN enrich_retail_source TEXT;
ALTER TABLE items ADD COLUMN enrich_matches_query INTEGER;
ALTER TABLE items ADD COLUMN enrich_risk          TEXT;
ALTER TABLE items ADD COLUMN enrich_verdict       TEXT;
ALTER TABLE items ADD COLUMN enriched_at          INTEGER;
