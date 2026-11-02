-- Where a salon's deposits go. The money is paid straight to the salon's own M-Pesa till or paybill:
-- RemboAI never holds anyone's money.
CREATE TABLE salon_payment (
    salon_id    uuid PRIMARY KEY REFERENCES salons ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('till', 'paybill')),
    shortcode   text NOT NULL,   -- Daraja BusinessShortCode. For a Daraja-backed till this is the head-office number.
    party_b     text NOT NULL,   -- who is paid: the till number, or the paybill number itself
    account_ref text NOT NULL DEFAULT 'RemboAI'
);

-- One row per M-Pesa prompt we send. Money states are explicit so nothing is ever lost silently:
--   pending -> paid | failed | needs_refund   (a late success may still arrive after failed)
CREATE TABLE payments (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id          uuid NOT NULL REFERENCES bookings,
    salon_id            uuid NOT NULL REFERENCES salons,
    phone               text NOT NULL,
    amount_kes          integer NOT NULL CHECK (amount_kes > 0),
    checkout_request_id text UNIQUE,
    merchant_request_id text,
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed', 'needs_refund')),
    result_code         integer,
    result_desc         text,
    mpesa_receipt       text,
    settled_via         text CHECK (settled_via IN ('callback', 'query')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    settled_at          timestamptz
);
CREATE UNIQUE INDEX payments_receipt ON payments (mpesa_receipt) WHERE mpesa_receipt IS NOT NULL;
CREATE UNIQUE INDEX payments_one_paid_per_booking ON payments (booking_id) WHERE status = 'paid';
CREATE INDEX payments_open ON payments (created_at) WHERE status = 'pending';
CREATE INDEX payments_booking ON payments (booking_id);

-- Every callback Safaricom sends, kept verbatim: an audit trail for disputes, and proof if a callback ever
-- arrives for a payment we cannot match.
CREATE TABLE mpesa_callbacks (
    id                  bigserial PRIMARY KEY,
    checkout_request_id text,
    body                jsonb NOT NULL,
    received_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX mpesa_callbacks_checkout ON mpesa_callbacks (checkout_request_id);
