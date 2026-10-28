-- A salon can be reached on WhatsApp, on Telegram, or both. The conversation engine is the same; only the channel differs.
ALTER TABLE salons ALTER COLUMN wa_phone_number_id DROP NOT NULL;
ALTER TABLE salons ADD COLUMN tg_bot_id text UNIQUE;   -- 'tg:<bot id>', the number before the colon in the bot token
ALTER TABLE salons ADD CONSTRAINT salons_has_a_channel CHECK (wa_phone_number_id IS NOT NULL OR tg_bot_id IS NOT NULL);
