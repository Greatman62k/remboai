# Owner API

One JSON API serves the owner's web page (`/owner`) and any native client (a Kotlin app would be the second client). Everything is scoped to the signed-in salon.

**Sign in.** The owner sends `link` to the salon's WhatsApp number and gets a single-use URL (valid 15 minutes). The token at the end of it is exchanged once:

`POST /api/owner/session {"token": "..."}` returns `{"session_token", "expires_at"}` (14 days) and also sets an HttpOnly, SameSite=Strict cookie for browsers.
Apps send `Authorization: Bearer <session_token>`. Cookie-authenticated writes must also send `X-Rembo: 1` (CSRF guard). `POST /api/owner/signout` ends the session.

| Method and path | What it does |
|---|---|
| `GET /me` | Salon name, hours, deposit, payment account |
| `PATCH /salon` | `name`, `deposit_kes`, `open_hours` (`{"mon": ["08:00","19:00"], "sun": null}`) |
| `GET /bookings?day=YYYY-MM-DD` | Confirmed bookings and live holds for a day, with deposit paid |
| `POST /bookings/{id}/cancel` | Cancels and tells the customer; returns any refund due |
| `GET /time_off`, `POST /time_off`, `DELETE /time_off/{id}` | Close a day or some hours; the POST reports bookings that clash |
| `GET/POST /services`, `PATCH /services/{id}` | Menu; `active: false` hides a service |
| `GET/POST /staff`, `PATCH /staff/{id}` | Stylists; `active: false` pauses bookings |
| `GET /summary` | Deposits today, refunds waiting |
| `GET /refunds`, `POST /refunds/{id}/done` | Payments that could not be honoured, to refund from the till by hand |

Errors are `{"detail": ...}` with 401 (not signed in), 403 (missing CSRF header), 404 (not yours or not found) or 422 (invalid input).
