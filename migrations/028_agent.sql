-- One row per customer message the agent handled: what the model asked for, what code answered, and what was sent.
-- It is the proof that the Meta model drives the conversation, and it lets a retried message resend the same reply.
CREATE TABLE IF NOT EXISTS agent_turns (
    id bigserial PRIMARY KEY,
    message_id text UNIQUE,
    salon_id uuid,
    sender text NOT NULL,
    user_text text NOT NULL DEFAULT '',
    steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    final_text text,
    buttons jsonb,
    state_after jsonb,
    outcome text NOT NULL,
    grounded boolean,
    problems jsonb,
    backend text,
    model text,
    model_calls int NOT NULL DEFAULT 0,
    ms int,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_turns_recent ON agent_turns (created_at DESC);
CREATE INDEX IF NOT EXISTS agent_turns_salon ON agent_turns (salon_id);
