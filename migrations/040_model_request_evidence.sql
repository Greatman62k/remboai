ALTER TABLE agent_turns
    ADD COLUMN IF NOT EXISTS model_requests jsonb NOT NULL DEFAULT '[]'::jsonb;
