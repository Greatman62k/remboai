-- Fence every webhook job lease to one specific claim. A stale worker must not
-- renew, complete, retry, or dead-letter a job after a new worker reclaims it.
ALTER TABLE webhook_jobs ADD COLUMN IF NOT EXISTS claim_token uuid;

-- Ignore exact M-Pesa callback replays in the audit table. Keep distinct bodies
-- for the same checkout so a forged or malformed first callback cannot hide a
-- later provider callback.
DELETE FROM mpesa_callbacks older
USING mpesa_callbacks newer
WHERE older.checkout_request_id IS NOT NULL
  AND older.checkout_request_id = newer.checkout_request_id
  AND older.body = newer.body
  AND older.id > newer.id;

CREATE UNIQUE INDEX IF NOT EXISTS mpesa_callbacks_dedupe
    ON mpesa_callbacks (checkout_request_id, (md5(body::text)))
    WHERE checkout_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS mpesa_callbacks_received_at
    ON mpesa_callbacks (received_at);
CREATE INDEX IF NOT EXISTS handoffs_retention
    ON handoffs ((COALESCE(resolved_at, created_at)));
CREATE INDEX IF NOT EXISTS unmatched_payments_raw_retention
    ON unmatched_payments (created_at) WHERE raw IS NOT NULL;
