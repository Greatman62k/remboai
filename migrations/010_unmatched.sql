CREATE TABLE IF NOT EXISTS unmatched_payments (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    checkout_request_id  text NOT NULL UNIQUE,
    merchant_request_id  text,
    result_code          integer NOT NULL,
    amount               integer,
    phone                text,
    mpesa_receipt        text,
    raw                  jsonb,
    status               text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution_note      text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    resolved_at          timestamptz
);
CREATE INDEX IF NOT EXISTS unmatched_payments_open_idx ON unmatched_payments (created_at) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS payments_unissued_idx ON payments (phone, amount_kes, created_at) WHERE status = 'pending' AND checkout_request_id IS NULL;
CREATE INDEX IF NOT EXISTS bookings_client_created_idx ON bookings (client_id, created_at);
