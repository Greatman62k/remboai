-- Every change of where customers' deposits go is requested, confirmed by a tap, and recorded.
CREATE TABLE IF NOT EXISTS salon_payment_changes (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id         uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    requested_by     text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('till', 'phone')),
    party_b          text NOT NULL,
    phone_type       text,
    previous_kind    text,
    previous_party_b text,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'declined', 'superseded', 'expired')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz
);
CREATE INDEX IF NOT EXISTS salon_payment_changes_salon_idx ON salon_payment_changes (salon_id, created_at DESC);
