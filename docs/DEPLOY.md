# Deploying RemboAI

Target: one small VPS (2 GB RAM is plenty), Ubuntu 24.04, a domain you control. Everything runs in Docker; Caddy gets and renews the HTTPS certificate by itself.

## 1. Server (once)
1. Create the VPS and point an **A record** for your domain (for example `rembo.example.com`) at its IP. Wait until `dig +short rembo.example.com` shows it.
2. `ssh root@SERVER`, then `git clone <your public repo> /opt/remboai && cd /opt/remboai`.
3. `bash deploy/server-setup.sh` (firewall with only 22/80/443, fail2ban, automatic security updates, swap, Docker).
4. Create a normal sudo user and disable root and password SSH logins if you plan to keep the server after the event.

## 2. Configuration
`cp .env.example .env` and fill it in, then `chmod 600 .env` so only your user can read the secrets. Beyond WhatsApp and model settings, set these values. Daraja credentials are needed only for automatic M-PESA STK; manual M-PESA and Airtel Money destinations are configured by each owner during registration:
- `DOMAIN` the domain above, `PUBLIC_BASE_URL=https://<DOMAIN>`
- `POSTGRES_PASSWORD` a long random string; `MPESA_CALLBACK_SECRET` another (`openssl rand -hex 24`)
- `WA_ACCESS_TOKEN` must be a **permanent system-user token** (Meta Business settings > System users). A temporary token dies after about 24 hours and the bot goes silent.

Assistant instructions live in `config/remboai_agent_policy.json`. Docker mounts this file into the app, so policy edits take effect on the next assistant turn without rebuilding or restarting the container. See [the agent policy notes](AGENT.md#remboai-assistant-policy).

Sensitive operational records are cleaned by the daily retention job: resolved/old handoffs after 30 days, callback bodies after 90 days (pending-payment callbacks are kept), and raw unmatched-payment payloads after 90 days. Adjust `HANDOFF_RETENTION_DAYS`, `MPESA_CALLBACK_RETENTION_DAYS`, and `UNMATCHED_RAW_RETENTION_DAYS` to match your operating policy.

## 3. Start
```bash
docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d --build
docker compose -f deploy/docker-compose.prod.yml logs -f app      # migrations apply on first start
curl https://$DOMAIN/ready
```

## 4. Create the salon
```bash
docker compose -f deploy/docker-compose.prod.yml exec app python scripts/seed_salon.py \
  --name "Brian's Kinyozi" --phone-number-id <ID from Meta API Setup> --stylist Brian \
  --service "Fade:500:45:kinyozi,cut" --service "Shave:300:20" \
  --till <HEAD_OFFICE>:<TILL> --owner 2547XXXXXXXX --deposit 200
docker compose -f deploy/docker-compose.prod.yml exec app python scripts/doctor.py
```
The command above configures Daraja STK. For manual payments, owners can register in chat and choose a bare seven-digit M-PESA till, a phone number explicitly marked as Pochi la Biashara or direct M-PESA Send Money, or an Airtel Money number. Customers receive route-specific instructions and send the amount and transaction reference in chat. The owner confirms only after checking the destination account.
`doctor` must end with "All set" (warnings about sandbox mode or failover are fine for a demo).
It also sends an in-memory synthetic M-PESA receipt with fake details to the configured vision endpoint and checks every extracted field. This tests image parsing without uploading customer payment evidence or creating a payment. The output names the model and reports whether its fields matched the fixture.

## 5. Connect Meta and Safaricom
- Meta developer dashboard > WhatsApp > Configuration: callback URL `https://<DOMAIN>/webhook`, verify token = `WA_VERIFY_TOKEN`, subscribe to **messages**.
- The Daraja callback URL is built from `PUBLIC_BASE_URL` and `MPESA_CALLBACK_SECRET`. For real-money Daraja STK, each salon's till or paybill must be approved for production (Go Live), with `MPESA_ENV=production` and the passkey Safaricom issues. Manual M-PESA and Airtel Money destinations do not use Daraja or send an STK prompt. The customer pays the chosen destination and sends a transaction reference; the owner verifies and confirms the claim.
- While the WhatsApp number is a test number, only recipients added under API Setup > To (maximum 5) can chat with it.

- Telegram (optional): see [TELEGRAM.md](TELEGRAM.md). Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`; with an https `PUBLIC_BASE_URL` the webhook is registered on start.

## 6. Voice notes (Modal, optional but recommended)
Customers send voice notes, often in Swahili or Sheng. Meta's open-source Omnilingual ASR (1,600+ languages, Swahili included) runs on a Modal GPU:
```bash
pip install modal && modal setup
modal secret create rembo-asr ASR_TOKEN=$(openssl rand -hex 24)
modal deploy deploy/modal_asr.py            # prints the web URL
modal run deploy/modal_asr.py --audio some_voice_note.ogg --lang swh_Latn     # try it on a real Swahili recording first
```
Put `ASR_URL=<url>/transcribe` and the same `ASR_TOKEN` in `.env`, restart the app, and run `doctor`. The GPU container sleeps after 5 minutes idle, so its first request after a quiet spell may be slower. If recognition is poor for a language mix, try `ASR_MODEL=omniASR_LLM_3B_v2` (needs a bigger GPU than the default L4 is comfortable with, edit `gpu=`) or `omniASR_CTC_1B_v2` for speed.

## 7. Backups and upkeep
- `echo '17 2 * * * root /opt/remboai/deploy/backup.sh' > /etc/cron.d/remboai-backup` for a nightly dump (14 days kept). Restore: `gunzip -c FILE | docker compose -f deploy/docker-compose.prod.yml exec -T db psql -U rembo rembo`.
- Update: `git pull && docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d --build` (new migrations apply on start).
- Logs: `docker compose -f deploy/docker-compose.prod.yml logs --since 1h app`. Look for `DELIVERY FAILED` (Meta delivery problems, with the reason), `REFUND NEEDED` (money to return), `rate limit`.
