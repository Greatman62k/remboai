CREATE TABLE IF NOT EXISTS handoffs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    client_id uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    category text NOT NULL,
    message text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS handoffs_one_open_per_client ON handoffs (client_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS handoffs_salon_open_idx ON handoffs (salon_id, created_at) WHERE status = 'open';
