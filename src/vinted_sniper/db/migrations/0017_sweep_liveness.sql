-- Liveness for sweep results, mirroring what 0016 gave `items`.
--
-- A sweep candidate never gets an `items` row (see 0014), so it could not inherit the
-- `sold_at` recheck at all: the /magic result page had no way to say a listing had gone.
-- These three columns are the same three `items` carries, so one wardrobe read can settle
-- both tables at once instead of costing a second request per seller.
--
-- `seller_id` is what makes that possible: the funnel already reads it off the catalog
-- item but only kept `seller_login`, which the wardrobe endpoint cannot be addressed by.
-- Rows written before this migration keep NULL here and are simply never due — there is
-- no way to recover the id for a listing that is already only a stored copy.
ALTER TABLE sweep_candidates ADD COLUMN seller_id INTEGER;
ALTER TABLE sweep_candidates ADD COLUMN sold_at INTEGER;
ALTER TABLE sweep_candidates ADD COLUMN sold_checked_at INTEGER;

-- The due-sellers scan groups still-live candidates by seller; without this it is a full
-- scan of every candidate ever swept, once an hour.
CREATE INDEX idx_sweep_candidates_liveness
    ON sweep_candidates(seller_id, sold_at, sold_checked_at);
