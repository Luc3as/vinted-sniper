-- A search can now say "only alert me when the AI agent scores a listing at least N".
-- The gate only applies when a verdict actually arrived in time: an agent that is down
-- or slow must never cost an alert, so an unscored listing goes out as before.
ALTER TABLE queries ADD COLUMN min_enrich_score INTEGER;
