ALTER TABLE salons ADD COLUMN IF NOT EXISTS wa_business_number text;
ALTER TABLE salons ADD COLUMN IF NOT EXISTS wa_access_token text;
ALTER TABLE salons ADD COLUMN IF NOT EXISTS ig_access_token text;
ALTER TABLE salons ADD COLUMN IF NOT EXISTS ig_user_id text;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'salons_wa_business_number_e164') THEN
        ALTER TABLE salons ADD CONSTRAINT salons_wa_business_number_e164 CHECK (wa_business_number IS NULL OR wa_business_number ~ '^[1-9][0-9]{6,14}$');
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS salons_wa_business_number_unique ON salons (wa_business_number) WHERE wa_business_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS salon_setup_invites (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash          text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    wa_phone_number_id  text NOT NULL UNIQUE,
    wa_business_number  text NOT NULL CHECK (wa_business_number ~ '^[1-9][0-9]{6,14}$'),
    expires_at          timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS salon_setup_invites_expiry_idx ON salon_setup_invites (expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS salon_setup_invites_wa_business_number_unique ON salon_setup_invites (wa_business_number);

ALTER TABLE services ADD COLUMN IF NOT EXISTS category text;
