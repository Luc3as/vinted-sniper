-- A notification now has a kind: the listing is new, or its price dropped. The outbox
-- was unique on (item, destination), which was right when the only news about a listing
-- was its arrival; a price can drop more than once, so the key grows to include what it
-- dropped from. SQLite cannot change a table constraint in place, hence the rebuild.
CREATE TABLE outbox_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          INTEGER NOT NULL,
    query_id         INTEGER NOT NULL,
    destination_id   INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  INTEGER NOT NULL,
    lease_expires_at INTEGER,
    last_error       TEXT,
    created_at       INTEGER NOT NULL,
    sent_at          INTEGER,
    kind             TEXT    NOT NULL DEFAULT 'new' CHECK (kind IN ('new', 'price_drop')),
    -- The total price before the drop; 0 for a plain "new listing" row so the unique
    -- key below still holds (NULLs would not compare equal).
    previous_price   REAL    NOT NULL DEFAULT 0,
    UNIQUE (item_id, destination_id, kind, previous_price)
);

INSERT INTO outbox_new (id, item_id, query_id, destination_id, status, attempts,
                        next_attempt_at, lease_expires_at, last_error, created_at, sent_at)
SELECT id, item_id, query_id, destination_id, status, attempts,
       next_attempt_at, lease_expires_at, last_error, created_at, sent_at
FROM outbox;

DROP TABLE outbox;
ALTER TABLE outbox_new RENAME TO outbox;
CREATE INDEX idx_outbox_claim ON outbox(destination_id, status, next_attempt_at);

-- When a listing's price last changed, so the dashboard can show it.
ALTER TABLE items ADD COLUMN price_changed_at INTEGER;
