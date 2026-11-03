# WhatsApp Templates for Reminders

WhatsApp requires approved Utility templates for reminders sent outside its 24-hour customer-service window. Telegram reminders are sent as direct messages and need no template.

## Set up the templates

In Meta Business Manager, create and get approval for the Utility templates your salon needs:

| Template name | Recipient | Purpose | Variables used by RemboAI |
|---|---|---|---|
| `reminder_24h` | Customer | Day-before appointment reminder | `name`, `service`, `date`, `time` |
| `reminder_2h` | Customer | Two-hour appointment reminder | `name`, `service`, `date`, `time` |
| `reminder_3h` | Customer | Keep/Cancel check about three hours before the visit | `name`, `service`, `date`, `time` |
| `reminder_soon` | Customer | ETA 30 minutes before the visit | `name`, `service`, `salon`, `minutes`, `when` |
| `owner_reminder_soon` | Owner | Customer ETA 30 minutes before the visit | `name`, `service`, `salon`, `minutes`, `when` |
| `owner_booking_cancelled` | Owner | Notice that a customer cancelled | `name`, `service`, `salon`, `when` |

For each approved template, add one `reminder_templates` row per salon. The stored `body` uses RemboAI placeholders such as `{name}`; `variables` lists those names in the same order as the approved template's numbered placeholders. Example:

```sql
INSERT INTO reminder_templates (salon_id, name, language, body, approved, variables)
VALUES (
  '<salon-uuid>', 'owner_reminder_soon', 'en',
  'Heads up: {name} is expected for {service} at {salon} in about {minutes} minutes ({when}).',
  true, '["name","service","salon","minutes","when"]'
);
```

The available values are `name`, `service`, `date`, `time`, `when`, `minutes`, and `salon`. The parameter order must match the Meta-approved template exactly, or Meta rejects the send with error 132000. Use the language code that matches the approved template.

## Scheduling and delivery

- For a confirmed booking more than 30 minutes away, RemboAI schedules one customer ETA and one owner ETA for 30 minutes before the appointment. WhatsApp needs the corresponding approved template for each recipient; Telegram sends the localized customer message and owner notice directly.
- If a booking is confirmed within 30 minutes of its start, the immediate booking confirmation and owner booking notice serve as the ETA. A second ETA is not queued.
- For bookings more than two hours away, the configured 24-hour and 2-hour customer reminders and the approved 3-hour Keep/Cancel check are also scheduled when their run times are still ahead. An explicit Cancel releases the slot to the waitlist. If the customer does not answer the 3-hour check, the booking remains confirmed.
- A customer cancellation queues one owner notice. WhatsApp needs an approved `owner_booking_cancelled` template; Telegram receives a direct message. Customer reminder opt-outs do not suppress owner notices.
- An approved `thanks_visit` template adds a thank-you at `ends_at+2h`, sent only if the booking is still confirmed after the visit. Customer opt-outs apply to customer reminders.
- The reminder worker runs every 60 seconds. Template sends use `POST /<phone_id>/messages`; a failed template can fall back to free text while WhatsApp's 24-hour window is open. Failed sends retry after 5 and 15 minutes, up to three attempts, and do not retry past the appointment start.
- Cancelling, expiring, or marking a booking as a no-show cancels its pending customer reminders. The owner ETA and cancellation notices use the salon's WhatsApp token when configured.

## Test

With `TEST_DATABASE_URL` configured:

```bash
pytest tests/test_reminders.py tests/test_still_coming.py -q
```
