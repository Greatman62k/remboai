-- Chat onboarding and salon binding: slugs for invite links, sender-to-salon bindings, registration limits.
ALTER TABLE salons ADD COLUMN IF NOT EXISTS slug text UNIQUE;

CREATE TABLE IF NOT EXISTS sender_salon (
    sender      text PRIMARY KEY,
    salon_id    uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    bound_at    timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL DEFAULT now() + interval '30 days'
);
CREATE INDEX IF NOT EXISTS sender_salon_salon_idx ON sender_salon (salon_id);

CREATE TABLE IF NOT EXISTS onboarding_attempts (
    sender      text NOT NULL,
    day         date NOT NULL DEFAULT CURRENT_DATE,
    count       integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sender, day)
);

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    sender      text PRIMARY KEY,
    state       jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
