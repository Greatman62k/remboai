-- Glance bearer sessions and one-time pairing challenges. Pairing challenges are
-- keyed with a server-side HMAC key; bearer tokens are stored only as SHA-256.
CREATE TABLE glance_pairing_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    owner_identity text NOT NULL,
    code_digest bytea NOT NULL CHECK (octet_length(code_digest) = 32),
    key_version integer NOT NULL CHECK (key_version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    revoked_at timestamptz,
    CONSTRAINT glance_pairing_code_digest_uniq UNIQUE (key_version, code_digest),
    CHECK (expires_at > created_at)
);
CREATE INDEX glance_pairing_owner_recent_idx
    ON glance_pairing_codes (owner_identity, created_at DESC);
CREATE INDEX glance_pairing_expiry_idx
    ON glance_pairing_codes (expires_at)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE glance_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    salon_id uuid NOT NULL REFERENCES salons(id) ON DELETE CASCADE,
    owner_identity text NOT NULL,
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    device_label text NOT NULL CHECK (length(device_label) BETWEEN 1 AND 40),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    last_used_at timestamptz,
    revoked_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX glance_sessions_salon_recent_idx
    ON glance_sessions (salon_id, created_at DESC);
CREATE INDEX glance_sessions_owner_active_idx
    ON glance_sessions (owner_identity, expires_at)
    WHERE revoked_at IS NULL;

-- Fixed-window counters are keyed by HMAC, independently by scope and subject.
-- There is deliberately no shared/global failure counter that can lock out all owners.
CREATE TABLE glance_rate_limits (
    scope text NOT NULL CHECK (scope IN ('exchange_source', 'exchange_owner', 'pair_generation_owner', 'read_session')),
    subject_hash bytea NOT NULL CHECK (octet_length(subject_hash) = 32),
    window_started_at timestamptz NOT NULL,
    count integer NOT NULL CHECK (count > 0),
    PRIMARY KEY (scope, subject_hash)
);

-- Future Glance approvals snapshot the session identifier and label. There is no
-- cascading FK: revoking or later pruning a session must not erase reply attribution.
ALTER TABLE outbound_outbox
    ADD COLUMN approved_glance_session_id uuid,
    ADD COLUMN approved_device_label text;
