CREATE TABLE IF NOT EXISTS webhook_jobs (
    id              bigserial PRIMARY KEY,
    provider        text NOT NULL CHECK (provider IN ('whatsapp', 'mpesa', 'telegram')),
    event_id        text NOT NULL,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'processed', 'dead')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at    timestamptz NOT NULL DEFAULT now(),
    locked_until    timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    UNIQUE (provider, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_jobs_ready_idx ON webhook_jobs (available_at, id) WHERE status IN ('pending', 'processing');
CREATE INDEX IF NOT EXISTS webhook_jobs_whatsapp_sender_pending_idx ON webhook_jobs ((payload->>'wa_id')) WHERE provider='whatsapp' AND status IN ('pending','processing');

ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN ('pending', 'paid', 'failed', 'needs_refund', 'review', 'refunded'));

CREATE TABLE IF NOT EXISTS payment_reviews (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id          uuid NOT NULL UNIQUE REFERENCES payments(id),
    reason              text NOT NULL CHECK (reason IN ('amount_mismatch', 'slot_unavailable', 'payment_unresolved')),
    status              text NOT NULL CHECK (status IN ('pending_review', 'refund_due', 'refunded', 'closed_no_refund')),
    expected_amount     integer NOT NULL,
    received_amount     integer,
    mpesa_receipt       text,
    resolution_reference text,
    resolution_note     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz
);
CREATE INDEX IF NOT EXISTS payment_reviews_open_idx ON payment_reviews (created_at) WHERE status IN ('pending_review', 'refund_due');

ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_reason text;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
