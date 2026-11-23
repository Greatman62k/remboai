-- Day 2 Glance workflow: durable draft idempotency, reply provenance and
-- replay data, the single-unresolved-reply invariant, and resolution audit.

ALTER TABLE handoff_drafts
    ADD COLUMN source text NOT NULL DEFAULT 'ai'
        CHECK (source IN ('ai', 'owner')),
    ADD COLUMN instruction text,
    ADD COLUMN superseded_at timestamptz;

ALTER TABLE glance_rate_limits DROP CONSTRAINT glance_rate_limits_scope_check;
ALTER TABLE glance_rate_limits ADD CONSTRAINT glance_rate_limits_scope_check
    CHECK (scope IN ('exchange_source', 'exchange_owner', 'pair_generation_owner', 'read_session', 'write_session'));

ALTER TABLE outbound_outbox
    ADD COLUMN reply_source text NOT NULL DEFAULT 'owner'
        CHECK (reply_source IN ('ai', 'ai_edited', 'owner')),
    ADD COLUMN approved_draft_text text,
    ADD COLUMN idempotency_method text NOT NULL DEFAULT 'POST',
    ADD COLUMN idempotency_resource_path text,
    ADD COLUMN request_payload_hash bytea,
    ADD COLUMN response_status integer,
    ADD COLUMN response_body jsonb;

UPDATE outbound_outbox o
SET idempotency_resource_path = '/api/v1/glance/cases/' || o.handoff_id::text || '/replies',
    approved_draft_text = (SELECT d.body FROM handoff_drafts d WHERE d.id = o.draft_id),
    reply_source = CASE
        WHEN o.draft_id IS NULL THEN 'owner'
        WHEN (SELECT d.source FROM handoff_drafts d WHERE d.id = o.draft_id) = 'owner' THEN 'owner'
        WHEN (SELECT e.body FROM handoff_events e WHERE e.id = o.approved_event_id) =
             (SELECT d.body FROM handoff_drafts d WHERE d.id = o.draft_id) THEN 'ai'
        ELSE 'ai_edited'
    END;

ALTER TABLE outbound_outbox
    ALTER COLUMN idempotency_resource_path SET NOT NULL;

ALTER TABLE outbound_outbox
    DROP CONSTRAINT outbound_outbox_salon_idempotency_uniq;
ALTER TABLE outbound_outbox
    ADD CONSTRAINT outbound_outbox_resource_idempotency_uniq
    UNIQUE (salon_id, idempotency_method, idempotency_resource_path, idempotency_key);

CREATE TABLE glance_request_idempotency (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    http_method text NOT NULL CHECK (http_method = 'POST'),
    resource_path text NOT NULL CHECK (length(resource_path) BETWEEN 1 AND 300),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) = 36),
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    state text NOT NULL CHECK (state IN ('in_progress', 'completed')),
    response_status integer,
    response_body jsonb,
    lease_until timestamptz,
    lease_token uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CONSTRAINT glance_request_idempotency_scope_uniq
        UNIQUE (salon_id, http_method, resource_path, idempotency_key),
    CHECK ((state = 'in_progress' AND response_status IS NULL AND response_body IS NULL)
        OR (state = 'completed' AND response_status IS NOT NULL AND response_body IS NOT NULL))
);
CREATE INDEX glance_request_idempotency_expiry_idx
    ON glance_request_idempotency (expires_at)
    WHERE state = 'completed';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM outbound_outbox
        WHERE state IN ('queued', 'sending', 'unknown')
        GROUP BY salon_id, handoff_id HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'migration 048 aborted: a case has multiple unresolved outbound replies';
    END IF;
END $$;

CREATE UNIQUE INDEX outbound_outbox_one_unresolved_per_case
    ON outbound_outbox (salon_id, handoff_id)
    WHERE state IN ('queued', 'sending', 'unknown');

ALTER TABLE handoffs ADD COLUMN resolution_result jsonb;

CREATE TABLE handoff_unknown_reply_acknowledgements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handoff_id uuid NOT NULL,
    salon_id uuid NOT NULL,
    client_id uuid NOT NULL,
    reply_id uuid NOT NULL UNIQUE REFERENCES outbound_outbox(id) ON DELETE CASCADE,
    owner_identity text NOT NULL,
    session_id uuid,
    device_label text,
    reason text NOT NULL CHECK (reason IN ('handled_in_person', 'customer_updated', 'no_action_needed', 'other')),
    note text,
    acknowledged_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT handoff_unknown_ack_case_fk
        FOREIGN KEY (handoff_id, salon_id, client_id)
        REFERENCES handoffs (id, salon_id, client_id) ON DELETE CASCADE
);

CREATE FUNCTION reject_unknown_reply_ack_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'unknown-reply acknowledgement records are immutable';
END $$;
CREATE TRIGGER handoff_unknown_reply_ack_immutable
    BEFORE UPDATE ON handoff_unknown_reply_acknowledgements
    FOR EACH ROW EXECUTE FUNCTION reject_unknown_reply_ack_update();
