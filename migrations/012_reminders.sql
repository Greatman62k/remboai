CREATE TABLE IF NOT EXISTS reminder_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid REFERENCES salons(id) ON DELETE CASCADE,
    name text NOT NULL,
    language text NOT NULL DEFAULT 'en',
    body text NOT NULL,
    approved boolean NOT NULL DEFAULT false,
    variables jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (salon_id, name)
);
CREATE INDEX IF NOT EXISTS reminder_templates_salon_idx ON reminder_templates (salon_id);

CREATE TABLE IF NOT EXISTS reminder_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    booking_id uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    wa_id text NOT NULL,
    run_at timestamptz NOT NULL,
    template_name text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS reminder_jobs_run_at_idx ON reminder_jobs (run_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS reminder_jobs_booking_idx ON reminder_jobs (booking_id);

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_opt_out boolean NOT NULL DEFAULT false;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS reminder_opt_out boolean NOT NULL DEFAULT false;
