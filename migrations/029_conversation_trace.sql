-- A complete, short-lived transcript for tuning: every inbound turn and each
-- outbound message, including deterministic responses that have no model trace.
CREATE TABLE IF NOT EXISTS conversation_trace (
    message_id   text PRIMARY KEY,
    thread       text NOT NULL,
    channel      text NOT NULL,
    message_kind text NOT NULL,
    user_text    text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_trace_recent ON conversation_trace (created_at DESC);
CREATE INDEX IF NOT EXISTS conversation_trace_thread ON conversation_trace (thread, created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_trace_replies (
    message_id   text NOT NULL REFERENCES conversation_trace(message_id) ON DELETE CASCADE,
    sequence     integer NOT NULL CHECK (sequence > 0),
    message_kind text NOT NULL,
    body         text NOT NULL DEFAULT '',
    buttons      jsonb,
    delivered    boolean,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (message_id, sequence)
);

-- Preserve the existing agent evidence in the new full-turn view. Past
-- deterministic replies cannot be reconstructed, so their absence stays clear.
INSERT INTO conversation_trace (message_id, thread, channel, message_kind, user_text, created_at)
SELECT message_id,
       CASE WHEN sender = 'owner' THEN 'owner:' || COALESCE(salon_id::text, 'unknown') ELSE sender END,
       'unknown', 'text', user_text, created_at
FROM agent_turns
WHERE message_id IS NOT NULL
ON CONFLICT (message_id) DO NOTHING;

INSERT INTO conversation_trace_replies (message_id, sequence, message_kind, body, delivered, created_at)
SELECT message_id, 1, 'text', final_text, NULL, created_at
FROM agent_turns
WHERE message_id IS NOT NULL AND final_text IS NOT NULL
ON CONFLICT (message_id, sequence) DO NOTHING;
