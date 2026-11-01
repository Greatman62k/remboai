ALTER TABLE conversation_trace ADD COLUMN IF NOT EXISTS salon_id uuid;

UPDATE conversation_trace c
SET salon_id = a.salon_id
FROM agent_turns a
WHERE a.message_id = c.message_id AND a.salon_id IS NOT NULL AND c.salon_id IS NULL;
