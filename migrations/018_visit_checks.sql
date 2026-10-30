-- Post-visit follow-up: one row per booking, asked once.
CREATE TABLE IF NOT EXISTS visit_checks (
    booking_id   uuid PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
    salon_id     uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    client_id    uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    asked_at     timestamptz NOT NULL DEFAULT now(),
    attended     boolean,
    feedback     text,
    answered_at  timestamptz
);
