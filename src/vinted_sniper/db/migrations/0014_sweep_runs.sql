-- Storage for the one-shot relevance sweep, kept deliberately apart from `items`.
--
-- A sweep reads existing stock in relevance order and ranks it; the standing poller reads
-- newest-first and alerts. They must never share a table: `Repo.known_item_ids()` looks up
-- `items` globally, so a listing written there by a sweep would be treated as already-seen
-- and the poller would silently never alert on it. Two tables is the structural guarantee
-- that a sweep can look at anything without blinding the watch.

-- sweep_runs: one row per sweep, from the moment it starts. It exists before there is any
-- saved search to attach it to (a sweep is how you decide whether the search is worth
-- keeping), so query_id is nullable and survives the search being deleted.
CREATE TABLE sweep_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id      INTEGER REFERENCES queries(id) ON DELETE SET NULL,
    tld           TEXT    NOT NULL,
    -- The catalog parameters actually sent, so a sweep can be read back and explained.
    params_json   TEXT    NOT NULL,
    -- Title keywords that ranked the candidates; kept because they rank rather than gate,
    -- and a run is only interpretable next to the words that scored it.
    keywords_json TEXT    NOT NULL DEFAULT '[]',
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    status        TEXT    NOT NULL DEFAULT 'running',
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    items_seen    INTEGER NOT NULL DEFAULT 0,
    candidates    INTEGER NOT NULL DEFAULT 0,
    -- How many listings each stage of the funnel let through, for the run log.
    funnel_json   TEXT    NOT NULL DEFAULT '{}',
    error         TEXT,
    -- What the AI stages spent. Zero until a later slice adds triage and verdicts.
    tokens        INTEGER NOT NULL DEFAULT 0,
    cost_eur      REAL    NOT NULL DEFAULT 0
);

-- sweep_candidates: the listings a sweep kept, with enough of the listing copied in to
-- render them later without re-fetching Vinted. `stage` and `reason` carry how far a
-- candidate got (funnel, triage, verdict) and why, so the AI stages can attach their
-- results in place rather than needing another table.
CREATE TABLE sweep_candidates (
    sweep_id        INTEGER NOT NULL REFERENCES sweep_runs(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL,
    rank_score      REAL    NOT NULL DEFAULT 0,
    -- Where the listing sat in the order it was handed to us, so ties stay reproducible.
    position        INTEGER NOT NULL,
    stage           TEXT    NOT NULL DEFAULT 'funnel',
    reason          TEXT,
    title           TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    price           REAL,
    total_price     REAL,
    currency        TEXT,
    brand           TEXT,
    size            TEXT,
    condition       TEXT,
    photo_url       TEXT,
    photo_urls_json TEXT    NOT NULL DEFAULT '[]',
    seller_login    TEXT,
    promoted        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sweep_id, item_id)
);

-- Reading a sweep always means "best first", never "all of them".
CREATE INDEX idx_sweep_candidates_rank ON sweep_candidates(sweep_id, rank_score DESC);
