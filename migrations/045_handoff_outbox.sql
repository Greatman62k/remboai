-- Case events are append-only. Retention may delete a case explicitly, but an
-- event can never be edited into a different customer-visible statement.
CREATE FUNCTION reject_handoff_event_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'handoff events are immutable';
END $$;
CREATE TRIGGER handoff_events_immutable
    BEFORE UPDATE ON handoff_events FOR EACH ROW EXECUTE FUNCTION reject_handoff_event_update();

CREATE TABLE outbound_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handoff_id uuid NOT NULL,
    salon_id uuid NOT NULL,
    client_id uuid NOT NULL,
    approved_event_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    recipient text NOT NULL,
    phone_number_id text,
    approved_by text NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    approved_case_version bigint NOT NULL CHECK (approved_case_version > 0),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','sending','submitted','delivered','failed','unknown','cancelled')),
    provider_message_id text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    claim_token uuid,
    lease_until timestamptz,
    last_error_category text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    delivered_at timestamptz,
    failed_at timestamptz,
    CONSTRAINT outbound_outbox_case_fk
        FOREIGN KEY (handoff_id,salon_id,client_id)
        REFERENCES handoffs (id,salon_id,client_id) ON DELETE RESTRICT,
    CONSTRAINT outbound_outbox_approved_event_fk
        FOREIGN KEY (approved_event_id,handoff_id,salon_id,client_id)
        REFERENCES handoff_events (id,handoff_id,salon_id,client_id) ON DELETE RESTRICT,
    CONSTRAINT outbound_outbox_salon_idempotency_uniq UNIQUE (salon_id,idempotency_key),
    CONSTRAINT outbound_outbox_approved_event_uniq UNIQUE (approved_event_id),
    CONSTRAINT outbound_outbox_phone_channel_check CHECK (
        (channel='telegram' AND phone_number_id IS NULL) OR
        (channel='whatsapp' AND phone_number_id IS NOT NULL)
    )
);

-- The dispatcher reads only queued records. Unknown and sending records can
-- never enter this index and are never blindly resent.
CREATE INDEX outbound_outbox_ready_idx ON outbound_outbox (available_at,created_at,id) WHERE state='queued';
CREATE INDEX outbound_outbox_expired_lease_idx ON outbound_outbox (lease_until) WHERE state='sending';
CREATE INDEX outbound_outbox_case_idx ON outbound_outbox (handoff_id,created_at DESC);
CREATE UNIQUE INDEX outbound_outbox_whatsapp_provider_id_uniq
    ON outbound_outbox (provider_message_id) WHERE channel='whatsapp' AND provider_message_id IS NOT NULL;
CREATE UNIQUE INDEX outbound_outbox_telegram_provider_id_uniq
    ON outbound_outbox (recipient,provider_message_id) WHERE channel='telegram' AND provider_message_id IS NOT NULL;
