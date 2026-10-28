-- Owner sign-in without passwords: the owner asks the WhatsApp number for a link; the link is single use and short lived.
-- Only hashes are stored, so a database leak does not hand out working sessions.
CREATE TABLE owner_links (
    token_hash text PRIMARY KEY,
    salon_id   uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

CREATE TABLE owner_sessions (
    token_hash text PRIMARY KEY,
    salon_id   uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX owner_sessions_salon ON owner_sessions (salon_id);

-- Refunds the owner has made by hand from the till are marked done, with the reason each one was needed.
ALTER TABLE payments DROP CONSTRAINT payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN ('pending', 'paid', 'failed', 'needs_refund', 'refunded'));
ALTER TABLE payments ADD COLUMN refund_reason text;
