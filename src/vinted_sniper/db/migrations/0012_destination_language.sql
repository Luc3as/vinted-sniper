-- The language a destination reads its alerts, bot replies and health notices in.
-- A Telegram chat in Slovak and a Discord server in English can share one instance.
ALTER TABLE destinations ADD COLUMN language TEXT NOT NULL DEFAULT 'en';
