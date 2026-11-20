-- Case history is separate from diagnostic traces. Old traces are deliberately
-- not imported because they do not reliably distinguish customer and owner text.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM handoffs h
        LEFT JOIN clients c ON c.id = h.client_id
        WHERE c.id IS NULL OR c.salon_id <> h.salon_id
    ) THEN
        RAISE EXCEPTION 'migration 044 aborted: handoffs contain a missing client or a client from another salon';
    END IF;
END $$;

ALTER TABLE clients
    ADD CONSTRAINT clients_id_salon_id_uniq UNIQUE (id, salon_id);

ALTER TABLE handoffs
    DROP CONSTRAINT handoffs_client_id_fkey,
    ADD CONSTRAINT handoffs_client_salon_fkey
        FOREIGN KEY (client_id, salon_id) REFERENCES clients (id, salon_id) ON DELETE CASCADE,
    ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    ADD COLUMN event_seq bigint NOT NULL DEFAULT 0 CHECK (event_seq >= 0),
    ADD COLUMN resolved_by text,
    ADD COLUMN resolution_reason text,
    ADD CONSTRAINT handoffs_id_salon_client_uniq UNIQUE (id, salon_id, client_id);

CREATE TABLE handoff_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handoff_id uuid NOT NULL,
    salon_id uuid NOT NULL,
    client_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN ('customer', 'assistant', 'owner', 'system')),
    visibility text NOT NULL CHECK (visibility IN ('customer_visible', 'owner_internal')),
    body text,
    channel text CHECK (channel IS NULL OR channel IN ('telegram', 'whatsapp')),
    provider_message_id text,
    related_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT handoff_events_case_fk
        FOREIGN KEY (handoff_id, salon_id, client_id)
        REFERENCES handoffs (id, salon_id, client_id) ON DELETE CASCADE,
    CONSTRAINT handoff_events_sequence_uniq UNIQUE (handoff_id, sequence),
    CONSTRAINT handoff_events_id_case_uniq UNIQUE (id, handoff_id, salon_id, client_id),
    CONSTRAINT handoff_events_related_same_case_fk
        FOREIGN KEY (related_event_id, handoff_id, salon_id, client_id)
        REFERENCES handoff_events (id, handoff_id, salon_id, client_id) ON DELETE CASCADE
);

-- handoffs.message is written only from the customer's triggering text by the
-- pre-event guard path. It is safe to expose as one initial customer event;
-- diagnostic conversation traces are intentionally not imported.
INSERT INTO handoff_events (handoff_id,salon_id,client_id,sequence,event_type,actor_type,visibility,body,channel,created_at)
SELECT h.id,h.salon_id,h.client_id,1,'customer_message','customer','customer_visible',h.message,
       CASE WHEN c.wa_id LIKE 'tg:%' THEN 'telegram' ELSE 'whatsapp' END,h.created_at
FROM handoffs h JOIN clients c ON c.id=h.client_id AND c.salon_id=h.salon_id
WHERE h.message IS NOT NULL AND btrim(h.message) <> '';
UPDATE handoffs h SET event_seq=1 WHERE h.event_seq=0 AND EXISTS (
    SELECT 1 FROM handoff_events e WHERE e.handoff_id=h.id AND e.sequence=1);

CREATE UNIQUE INDEX handoff_events_provider_message_uniq
    ON handoff_events (salon_id, channel, provider_message_id)
    WHERE provider_message_id IS NOT NULL AND event_type = 'customer_message';
CREATE UNIQUE INDEX handoff_events_related_reply_uniq
    ON handoff_events (handoff_id, related_event_id, event_type, actor_type)
    WHERE related_event_id IS NOT NULL;
CREATE INDEX handoff_events_customer_context_idx
    ON handoff_events (handoff_id, sequence DESC)
    WHERE visibility = 'customer_visible';
CREATE INDEX handoffs_client_salon_idx ON handoffs (client_id,salon_id);
