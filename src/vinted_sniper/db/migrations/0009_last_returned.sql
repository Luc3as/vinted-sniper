-- How many listings the last check returned. A search whose page is not even full is a
-- niche one, and its quiet is the market's, not a frozen feed's.
ALTER TABLE query_state ADD COLUMN last_returned INTEGER;
