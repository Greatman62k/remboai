-- Short-lived, sender-scoped context for the platform admin's conversational assistant.
CREATE TABLE IF NOT EXISTS platform_agent_sessions (
    sender     text PRIMARY KEY,
    history    jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
