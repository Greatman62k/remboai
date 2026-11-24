-- Day 3 Glance ASR quota: add a dedicated per-session scope to the existing
-- HMAC-keyed rate-limit table. No new rate-limit store or subject data is added.
ALTER TABLE glance_rate_limits DROP CONSTRAINT glance_rate_limits_scope_check;
ALTER TABLE glance_rate_limits ADD CONSTRAINT glance_rate_limits_scope_check
    CHECK (scope IN (
        'exchange_source', 'exchange_owner', 'pair_generation_owner',
        'read_session', 'write_session', 'transcription_session'
    ));
