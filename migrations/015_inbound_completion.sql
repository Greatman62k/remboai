-- A message used to count as handled the moment it arrived, so a crash while handling it lost it for good.
-- Now it is only "done" when completed_at is set; a message seen but not completed is picked up again by its (retried) job.
ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS completed_at timestamptz;
-- Everything recorded before this migration was handled under the old rules.
UPDATE inbound_messages SET completed_at = received_at WHERE completed_at IS NULL;
