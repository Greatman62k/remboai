-- A deletion can proceed only after the same platform administrator reviews and confirms one exact business.
CREATE TABLE IF NOT EXISTS platform_delete_confirmations (
    sender       text PRIMARY KEY,
    salon_id     uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS platform_delete_confirmations_requested_at_idx
    ON platform_delete_confirmations (requested_at);
