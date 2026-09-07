-- What the two AI stages of a sweep conclude, attached in place to the candidates that
-- 0014 already stored.
--
-- Both stages update rows that exist before they run: the funnel writes its survivors,
-- then triage judges them, then a verdict judges the best few. Losing an earlier stage's
-- answer because a later one failed would throw away work that was already paid for, so
-- these are columns on `sweep_candidates` rather than a re-inserted, truncated set.

-- The small photo variant the triage stage is billed on. Kept next to `photo_url` rather
-- than replacing it: the verdict stage still wants the full-size photos, and the two
-- stages have very different price tags per pixel.
ALTER TABLE sweep_candidates ADD COLUMN thumb_url TEXT;

-- Thumbnail triage. `matches_target` is deliberately nullable and three-valued:
--   NULL = never triaged (the batch failed, or the flow omitted this id),
--   0    = triaged and rejected,
--   1    = triaged and recognised.
-- The distinction is load-bearing for the re-rank: an un-triaged item sorts below a
-- confirmed match but above a rejection, so a flow that drops an id cannot silently
-- demote that listing to "not what you asked for".
ALTER TABLE sweep_candidates ADD COLUMN matches_target INTEGER;
-- 0..1 from the flow. Confidence ranks; it never gates.
ALTER TABLE sweep_candidates ADD COLUMN confidence REAL;
ALTER TABLE sweep_candidates ADD COLUMN triage_reason TEXT;

-- The full photo verdict, the same answer shape `items`' enrich_* columns hold, stored
-- here instead of there on purpose: a sweep candidate has no `items` row and must never
-- gain one (D005/D009). Writing a sweep result into `items` would make the standing
-- poller treat that listing as already-seen and never alert on it. Same reason 0014 gave
-- sweeps their own tables; these columns are that decision carried through to the answer.
ALTER TABLE sweep_candidates ADD COLUMN verdict_score INTEGER;
ALTER TABLE sweep_candidates ADD COLUMN verdict_model TEXT;
ALTER TABLE sweep_candidates ADD COLUMN verdict_retail_price REAL;
ALTER TABLE sweep_candidates ADD COLUMN verdict_retail_source TEXT;
ALTER TABLE sweep_candidates ADD COLUMN verdict_matches_query INTEGER;
ALTER TABLE sweep_candidates ADD COLUMN verdict_risk TEXT;
-- The one line written for a human. Named `verdict_text` because `verdict` alone would
-- read as "was there a verdict"; `judged_at` is what answers that.
ALTER TABLE sweep_candidates ADD COLUMN verdict_text TEXT;
ALTER TABLE sweep_candidates ADD COLUMN judged_at INTEGER;

-- Note: `sweep_runs.tokens` and `sweep_runs.cost_eur` already exist from 0014, and every
-- column above is nullable because SQLite's ALTER TABLE ADD COLUMN cannot add NOT NULL
-- without a default. Here that is the right shape anyway: NULL means "no answer yet".
