-- Filters Vinted's own search cannot express. Words that must all appear in the title, a
-- regular expression the title must match, a floor on the seller's rating and review
-- count, and sellers to skip outright. All optional; rows written before this migration
-- keep NULL / '[]' and behave exactly as they did.
ALTER TABLE queries ADD COLUMN required_keywords_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE queries ADD COLUMN title_pattern TEXT;
ALTER TABLE queries ADD COLUMN min_seller_rating REAL;
ALTER TABLE queries ADD COLUMN min_seller_reviews INTEGER;
ALTER TABLE queries ADD COLUMN blocked_sellers_json TEXT NOT NULL DEFAULT '[]';
