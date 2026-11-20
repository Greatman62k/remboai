CREATE TABLE handoff_drafts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handoff_id uuid NOT NULL,
    salon_id uuid NOT NULL,
    client_id uuid NOT NULL,
    based_case_version bigint NOT NULL CHECK (based_case_version > 0),
    requested_by text NOT NULL,
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 700),
    model_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    CONSTRAINT handoff_drafts_case_fk
        FOREIGN KEY (handoff_id,salon_id,client_id)
        REFERENCES handoffs (id,salon_id,client_id) ON DELETE CASCADE,
    CONSTRAINT handoff_drafts_id_case_uniq UNIQUE (id,handoff_id,salon_id,client_id)
);
CREATE INDEX handoff_drafts_case_recent_idx ON handoff_drafts (handoff_id,created_at DESC);
CREATE INDEX handoff_drafts_expiry_idx ON handoff_drafts (expires_at) WHERE consumed_at IS NULL;

ALTER TABLE outbound_outbox ADD COLUMN draft_id uuid;
ALTER TABLE outbound_outbox
    ADD CONSTRAINT outbound_outbox_draft_fk
        FOREIGN KEY (draft_id,handoff_id,salon_id,client_id)
        REFERENCES handoff_drafts (id,handoff_id,salon_id,client_id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX outbound_outbox_draft_uniq ON outbound_outbox (draft_id) WHERE draft_id IS NOT NULL;
