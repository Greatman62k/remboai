-- Keep owner-verification claims discoverable after a booking's 30-minute hold expires.
ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS customer_expired_notice_at timestamptz;

CREATE INDEX IF NOT EXISTS payments_manual_claim_inbox_idx
    ON payments (salon_id, created_at)
    WHERE method = 'manual' AND status = 'pending';

-- Manual-payment bookings require a live owner availability check before deposit instructions.
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS owner_check_status text
        CHECK (owner_check_status IN ('pending', 'approved', 'declined', 'timed_out', 'late_approved',
                                      'customer_expired', 'customer_accepted', 'customer_declined')),
    ADD COLUMN IF NOT EXISTS owner_check_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS owner_check_responded_at timestamptz,
    ADD COLUMN IF NOT EXISTS owner_customer_notice_at timestamptz,
    ADD COLUMN IF NOT EXISTS owner_check_timeout_notice_at timestamptz,
    ADD COLUMN IF NOT EXISTS owner_late_notice_at timestamptz,
    ADD COLUMN IF NOT EXISTS customer_reply_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS bookings_owner_check_pending_idx
    ON bookings (owner_check_expires_at)
    WHERE owner_check_status = 'pending' AND status = 'held';

CREATE INDEX IF NOT EXISTS bookings_owner_late_pending_idx
    ON bookings (customer_reply_expires_at)
    WHERE owner_check_status = 'late_approved';
