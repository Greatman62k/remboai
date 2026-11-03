-- Preserve the explicit ops disposition on the payment row so closed claims
-- leave the unresolved owner inbox without being mistaken for a failed transfer.
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_status_check;
ALTER TABLE payments
    ADD CONSTRAINT payments_status_check
    CHECK (status IN ('pending', 'paid', 'failed', 'needs_refund', 'review', 'refunded', 'closed_no_refund'));
