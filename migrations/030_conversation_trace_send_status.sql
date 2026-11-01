-- This is a provider send result, not proof the recipient opened or received it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='conversation_trace_replies' AND column_name='delivered'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='conversation_trace_replies' AND column_name='send_ok'
    ) THEN
        ALTER TABLE conversation_trace_replies RENAME COLUMN delivered TO send_ok;
    END IF;
END $$;
