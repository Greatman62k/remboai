-- Time the salon is closed outside its normal hours: holidays, a funeral, an afternoon off.
-- staff_id NULL means the whole salon; a stylist's own time off can use the same table later.
CREATE TABLE time_off (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id   uuid NOT NULL REFERENCES salons ON DELETE CASCADE,
    staff_id   uuid REFERENCES staff ON DELETE CASCADE,
    starts_at  timestamptz NOT NULL,
    ends_at    timestamptz NOT NULL,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at)
);
CREATE INDEX time_off_salon ON time_off (salon_id, starts_at);

-- The owner's end-of-day summary is sent once per local day.
ALTER TABLE salons ADD COLUMN last_summary_on date;
