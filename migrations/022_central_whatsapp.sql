-- One central WhatsApp number serves many salons, like the shared Telegram bot.
-- The earliest salon on a number is the default; sender_salon bindings route the rest.
ALTER TABLE salons DROP CONSTRAINT IF EXISTS salons_wa_phone_number_id_key;
CREATE INDEX IF NOT EXISTS salons_wa_pid_idx ON salons (wa_phone_number_id);
ALTER TABLE salon_setup_invites DROP CONSTRAINT IF EXISTS salon_setup_invites_wa_phone_number_id_key;
CREATE INDEX IF NOT EXISTS salon_setup_invites_pid_idx ON salon_setup_invites (wa_phone_number_id);
