-- A window each day during which a destination is not sent anything. Alerts found in the
-- window are kept and delivered when it ends, rather than expiring, so the morning
-- brings a digest of the night instead of silence. NULL means always on.
ALTER TABLE destinations ADD COLUMN quiet_hours TEXT;
