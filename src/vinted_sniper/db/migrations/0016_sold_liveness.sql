-- Whether a recorded listing is still up on Vinted. `sold_at` is when the liveness
-- recheck concluded it is gone — sold or withdrawn, the anonymous API cannot tell which,
-- so the UI must not claim more. `sold_checked_at` is when the wardrobe was last read,
-- so a cautious "could not tell" still pushes the next look ~6h out.
ALTER TABLE items ADD COLUMN sold_at INTEGER;
ALTER TABLE items ADD COLUMN sold_checked_at INTEGER;
