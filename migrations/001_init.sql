-- Meta redelivers webhooks; the primary key makes each WhatsApp message id run exactly once.
CREATE TABLE inbound_messages (
    wa_message_id text PRIMARY KEY,
    wa_id         text NOT NULL,
    received_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX inbound_messages_received_at ON inbound_messages (received_at);
