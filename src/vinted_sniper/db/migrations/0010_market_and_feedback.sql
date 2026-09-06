-- Three kinds of memory that make a verdict about "is this a bargain?" rest on facts.
--
-- market: every listing a search's page has ever shown, whether or not it passed the
-- filters, with its price and how long it stayed visible. Prices across a few hundred of
-- these say what the thing actually sells for second-hand — which retail price does not.
CREATE TABLE market (
    item_id         INTEGER NOT NULL,
    query_id        INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    price           REAL,
    total_price     REAL,
    currency        TEXT,
    size            TEXT,
    condition       TEXT,
    brand           TEXT,
    photo_ts        INTEGER,
    favourite_count INTEGER,
    view_count      INTEGER,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    PRIMARY KEY (item_id, query_id)
);
CREATE INDEX idx_market_query_seen ON market(query_id, last_seen_at);

-- retail_prices: what the agent found a product costs new, kept so the next listing of
-- the same product does not cost another web search and gets the same number.
CREATE TABLE retail_prices (
    query_id   INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    model      TEXT    NOT NULL,
    price      REAL    NOT NULL,
    currency   TEXT,
    source     TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (query_id, model)
);

-- verdict_feedback: the buyer's thumbs up or down on a verdict. Fed back to the agent as
-- examples of what this particular buyer calls a deal.
CREATE TABLE verdict_feedback (
    item_id  INTEGER PRIMARY KEY,
    query_id INTEGER NOT NULL,
    rating   INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    rated_at INTEGER NOT NULL
);
