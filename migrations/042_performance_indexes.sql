-- Found by measuring a database with 1.26M bookings and 3M inbound messages (about 173k a day).
-- Without these, each of the queries below scanned a whole table.

-- Per-sender message count, run twice for every inbound message: 24 ms -> under 1 ms.
CREATE INDEX IF NOT EXISTS inbound_messages_sender_recent ON inbound_messages (wa_id, received_at);

-- A salon's bookings for a day or range (owner agenda, summaries, salon lookups, salon deletion): 151 ms -> under 1 ms.
CREATE INDEX IF NOT EXISTS bookings_salon_starts_idx ON bookings (salon_id, starts_at);

-- "Was the customer attended?" check, run every 30 seconds over the bookings that just ended: 145 ms -> under 1 ms.
CREATE INDEX IF NOT EXISTS bookings_confirmed_ends_idx ON bookings (ends_at) WHERE status = 'confirmed';
