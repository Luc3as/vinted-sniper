-- A search can now say "only tell me about the cheapest N% of what this search has been
-- seeing", and every recorded listing remembers where its price sat when it was found,
-- so the alert can say "cheaper than 88% of the 312 listings seen this month".
ALTER TABLE queries ADD COLUMN max_market_percentile INTEGER;
ALTER TABLE items ADD COLUMN market_percentile INTEGER;
ALTER TABLE items ADD COLUMN market_n INTEGER;
