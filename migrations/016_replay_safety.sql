-- 1. Per-sender lock as a lease row instead of a session advisory lock: handling a message no longer pins a pooled connection
--    for as long as the model call and the WhatsApp sends take.
CREATE TABLE IF NOT EXISTS sender_locks (
    key          text PRIMARY KEY,
    owner        uuid NOT NULL,
    locked_until timestamptz NOT NULL
);

-- 2. One sender's messages are handled in the order they arrived (see claim_next).
ALTER TABLE webhook_jobs ADD COLUMN IF NOT EXISTS ordering_key text;
UPDATE webhook_jobs SET ordering_key = COALESCE(payload->>'phone_number_id', '') || ':' || COALESCE(payload->>'wa_id', payload->>'sender')
 WHERE provider IN ('whatsapp', 'telegram') AND ordering_key IS NULL AND COALESCE(payload->>'wa_id', payload->>'sender') IS NOT NULL;
CREATE INDEX IF NOT EXISTS webhook_jobs_ordering_idx ON webhook_jobs (ordering_key, id) WHERE ordering_key IS NOT NULL AND status IN ('pending', 'processing');

-- 3. A retried message must not repeat what the first attempt already did.
--    The conversation as it was before the first attempt touched it (restored on a retry):
ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS state_before jsonb;
--    Messages already sent while handling one inbound message (digest = hash of recipient + text; occ = nth identical send):
CREATE TABLE IF NOT EXISTS outbound_log (
    message_id text NOT NULL,
    digest     text NOT NULL,
    occ        integer NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (message_id, digest, occ)
);
CREATE INDEX IF NOT EXISTS outbound_log_sent_at ON outbound_log (sent_at);
--    Effects keyed by the message that caused them:
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS source_message_id text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancel_message_id text;
CREATE UNIQUE INDEX IF NOT EXISTS bookings_source_message_uniq ON bookings (source_message_id) WHERE source_message_id IS NOT NULL AND status IN ('held', 'confirmed');
CREATE INDEX IF NOT EXISTS bookings_cancel_message ON bookings (cancel_message_id) WHERE cancel_message_id IS NOT NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS source_message_id text;
CREATE INDEX IF NOT EXISTS payments_source_message ON payments (source_message_id) WHERE source_message_id IS NOT NULL;
ALTER TABLE abuse_strikes ADD COLUMN IF NOT EXISTS message_id text;
CREATE UNIQUE INDEX IF NOT EXISTS abuse_strikes_message_uniq ON abuse_strikes (message_id, reason) WHERE message_id IS NOT NULL;

-- 4. A settled payment is announced to the customer and the owner at least once: if announcing failed, the retry (which finds the
--    payment already settled) still announces it. Payments settled before this migration are taken to have been announced.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS announced_at timestamptz;
UPDATE payments SET announced_at = COALESCE(settled_at, now()) WHERE status <> 'pending' AND announced_at IS NULL;
