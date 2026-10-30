# WhatsApp Templates for Reminders

Reminders go outside the 24h customer-service window, so they must be approved Utility templates.

## Setup in Meta Business Manager
1. Business Settings → WhatsApp Manager → Message Templates → Create.
2. Category: **Utility**, Name: `reminder_24h`, Language: `en` (and `sw` if needed).
3. Body: `Hello {1}, reminder: {2} on {3} at {4}. Reply STOP to opt out.`
   Variables: 1=customer name, 2=service, 3=date, 4=time.
4. Submit for approval (usually 24h). Do the same for `reminder_2h`.
5. Set `REMINDER_TEMPLATE_24H`/`REMINDER_TEMPLATE_2H` in `.env` to the approved names, restart.
6. Add a row per salon to `reminder_templates` with `approved=true` and `variables` listing the variables in order, so the parameters sent match the template Meta approved:
   `INSERT INTO reminder_templates (salon_id, name, language, body, approved, variables) VALUES (..., 'reminder_24h', 'en', 'Hello {name}, reminder: {service} on {date} at {time}.', true, '["name","service","date","time"]');`
   Available variables: `name`, `service`, `date`, `time`, `when`, `salon`. A row with no `variables` sends the whole rendered `body` as a single parameter (only right for a template whose body is one variable).
   Meta rejects a send whose parameter count differs from the approved template (error 132000), so the list must match.

## How Rembo uses them

- On `paid` booking, `schedule_reminders` creates jobs at `starts_at-24h` and `-2h` if >2h away and not opt-out.
- A salon that adds an approved `thanks_visit` template gets a post-visit thank-you: one job at `ends_at+2h`, sent only if the booking is still `confirmed` once the visit is over (cancelled, expired and no-show bookings get no thanks). Opt-outs apply as usual.
- `run_reminders` every 60s sends via `POST /<phone_id>/messages` `type: template` with `body` parameters; fallback to `text` inside 24h window if template fails. A send that fails is retried (5 and 15 minutes later, 3 attempts in all, never past the appointment) before the job is marked `failed`. The salon's own token is decrypted before use.
- Cancel on `cancelled/expired/no_show` → `update reminder_jobs status=cancelled`.
- Customer `STOP`/`opt out` → `clients.reminder_opt_out=true`, future jobs cancelled, no new jobs.
- Owner `noshow #2` → `bookings.status=no_show`, reminders cancelled, owner told.

## Testing

Use sandbox templates (auto-approved) or approved test templates. Verify with `TEST_DATABASE_URL`:
```
pytest tests/test_reminders.py -q
```
