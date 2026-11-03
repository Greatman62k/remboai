-- Keep amount-mismatch manual claims searchable from the owner payment inbox.
CREATE INDEX IF NOT EXISTS payments_manual_review_claim_inbox_idx
    ON payments (salon_id, created_at)
    WHERE method = 'manual' AND status = 'review';
