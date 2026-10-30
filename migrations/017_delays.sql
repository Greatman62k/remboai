-- One owner lateness announcement: which stylist, which day, how many minutes.
-- The owner message id is unique, so redelivering the same command shifts nothing twice.
CREATE TABLE IF NOT EXISTS delays (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id      uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    staff_id      uuid NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    day           date NOT NULL,
    minutes       integer NOT NULL,
    message_id    text UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (salon_id, staff_id, day, minutes)
);
CREATE INDEX IF NOT EXISTS delays_salon_day_idx ON delays (salon_id, day);
