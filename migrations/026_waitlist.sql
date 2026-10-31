-- Waitlist fill: when a booking is cancelled, the freed slot is offered to
-- waiting customers in join order. The first to tap Yes within 10 minutes
-- books it; No, expiry, or a taken slot moves the offer to the next waiter.
CREATE TABLE IF NOT EXISTS waitlist (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    service_id uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    day date NOT NULL,
    status text NOT NULL DEFAULT 'waiting'
        CHECK (status IN ('waiting', 'offered', 'booked', 'expired', 'declined', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    notified_at timestamptz,
    offer_staff_id uuid REFERENCES staff(id) ON DELETE SET NULL,
    offer_starts_at timestamptz,
    offer_ends_at timestamptz,
    offer_expires_at timestamptz
);
CREATE INDEX IF NOT EXISTS waitlist_salon_service_day
    ON waitlist (salon_id, service_id, day, status, created_at);
