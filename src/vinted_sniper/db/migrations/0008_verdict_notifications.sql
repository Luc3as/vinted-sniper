-- A third kind of notification: a verdict that arrived after the listing's alert had
-- already gone out. Only sent for hot deals — a late "meh" is not worth a second message.
-- Same rebuild dance as 0006: SQLite cannot widen a CHECK in place.
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
    kind             TEXT    NOT NULL DEFAULT 'new' CHECK (kind IN ('new', 'price_drop', 'verdict')),
    previous_price   REAL    NOT NULL DEFAULT 0,
    UNIQUE (item_id, destination_id, kind, previous_price)
);

INSERT INTO outbox_new SELECT * FROM outbox;
DROP TABLE outbox;
ALTER TABLE outbox_new RENAME TO outbox;
CREATE INDEX idx_outbox_claim ON outbox(destination_id, status, next_attempt_at);
