-- Inactivity warnings: set when the day-7 and day-13 notices go out, cleared when
-- bookings resume. A salon with neither warning sent gets its first notice on schedule.
ALTER TABLE salons ADD COLUMN IF NOT EXISTS inactivity_warned_at timestamptz;
ALTER TABLE salons ADD COLUMN IF NOT EXISTS inactivity_final_at timestamptz;
