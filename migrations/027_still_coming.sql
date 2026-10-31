-- Still-coming 3h reminders: Keep/Cancel taps, and a no-reply release that
-- frees the slot toward the waitlist. replied marks a Keep tap so the
-- release sweep only takes bookings the customer never confirmed back.
ALTER TABLE reminder_jobs ADD COLUMN IF NOT EXISTS replied boolean NOT NULL DEFAULT false;
