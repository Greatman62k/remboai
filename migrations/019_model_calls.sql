-- Every model call the gateway makes, for the accuracy/latency/cost table and debugging.
CREATE TABLE IF NOT EXISTS model_calls (
    id          bigserial PRIMARY KEY,
    backend     text NOT NULL,
    model       text NOT NULL,
    step        text NOT NULL DEFAULT 'extract',
    tokens_in   integer,
    tokens_out  integer,
    latency_ms  integer,
    ok          boolean NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS model_calls_backend_idx ON model_calls (backend, created_at);
