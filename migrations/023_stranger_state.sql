-- Senders the shared bot met before any business existed: a repeat counter so the
-- intro does not loop forever, and a short transcript so their visits stay checkable.
CREATE TABLE IF NOT EXISTS stranger_state (
    sender     text NOT NULL PRIMARY KEY,
    state      jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
