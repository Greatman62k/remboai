-- Daily counters for the paid or metered calls (language model, photo matching, speech), so a flood of messages cannot run up a bill.
CREATE TABLE model_usage (
    day   date NOT NULL,
    kind  text NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);

