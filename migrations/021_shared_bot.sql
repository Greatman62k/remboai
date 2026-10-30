-- One Telegram bot serves many salons: the bot id is no longer unique per salon.
-- The earliest salon on a number is the default; sender_salon bindings route the rest.
ALTER TABLE salons DROP CONSTRAINT IF EXISTS salons_tg_bot_id_key;
CREATE INDEX IF NOT EXISTS salons_tg_bot_idx ON salons (tg_bot_id);
ALTER TABLE salons ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
