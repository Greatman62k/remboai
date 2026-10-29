-- One row per abuse signal (an injection attempt, a burst of messages). Enough recent strikes and the sender is ignored for a day.
CREATE TABLE abuse_strikes (
    id      bigserial PRIMARY KEY,
    sender  text NOT NULL,
    reason  text NOT NULL,
    at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX abuse_strikes_sender_at ON abuse_strikes (sender, at);

