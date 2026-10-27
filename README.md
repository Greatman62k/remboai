# RemboAI

A WhatsApp receptionist for Kenyan salons and barbershops. A customer writes the way they talk, in English, Swahili or Sheng;
the receptionist finds a free slot, holds it, and asks for an M-Pesa deposit. Built for the Meta Global AI Developer Hackathon,
problem statement **Build for Your World**.

> **Prior work and what's new.** Before the event I privately prototyped an earlier version of this idea (not submitted, not public,
> and none of its code is in this repository), and I set up my WhatsApp Business Cloud API and Daraja (M-Pesa) sandbox accounts.
> This repository is a fresh implementation, written Oct 27 - Nov 4, 2026. What carried over is the idea and the design.
> Every file here was written during the event; see [CHANGELOG.md](CHANGELOG.md).

## Status
Day 1: signed WhatsApp webhook that echoes messages. Booking, M-Pesa and the Meta models arrive in the following days.

## Run locally
```bash
cp .env.example .env          # fill in your own values
docker compose up --build     # Postgres + the app on :8080
python -m pytest -q           # tests (database tests need TEST_DATABASE_URL)
```

## Layout
- `app/whatsapp.py` signature check, payload parsing, sending, explanations of Meta's error codes
- `app/db.py` connection pool and a small migration runner (`migrations/*.sql`, applied in order, once)
- `app/main.py` the web app: `/health`, `/webhook`

## Security notes
Every webhook is verified with the app secret (HMAC-SHA256) before anything is read. Meta may deliver a message more than once;
each message id is stored and processed exactly once. Phone numbers are masked in logs. Secrets live only in `.env`, which is never committed.

## Licence
MIT, see [LICENSE](LICENSE).
