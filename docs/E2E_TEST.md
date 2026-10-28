# End-to-end test day

Goal: prove, on real services, every step a customer and an owner will take. Work top to bottom; stop at the first red line and send the output.

## 0. Start
```bash
cp .env.example .env            # LLM_API_KEY, MPESA_* (sandbox), TELEGRAM_BOT_TOKEN, ASR_* if Modal is deployed
docker compose up --build       # Postgres + app on :8080 (migrations run on start)
docker compose exec app python scripts/seed_salon.py --name "Test Cuts" --telegram-bot-id <BOT_ID> --stylist Brian \
  --service "Fade:500:45:kinyozi,cut" --service "Silk press:2500:90" --till 174379:5012345 --owner tg:<YOUR_ID> --deposit 200
docker compose exec app python scripts/doctor.py
```
`doctor` lists what is not ready. Models must say OK before going on; M-Pesa and speech may be skipped or warn.

## 1. Telegram, no public URL needed
On your phone, in your bot:
1. `hi` then `nataka fade kesho mchana` -> slot buttons in Swahili. Tap one.
2. The bot asks for an M-Pesa number. Send `0712345678` (any number; the sandbox sends nothing to a phone).
3. Send a voice note: "nataka fade kesho". Expect `Nimesikia: "..."` or `I heard: "..."` (needs `ASR_URL`).
4. Send a photo of a haircut with the caption `kesho mchana`. Expect a suggested service from the menu and then slots (needs the model key).
5. As the owner, send `today`, `summary`, `link`. Open the link on your phone: bookings, services, time off.

## 2. M-Pesa sandbox
The sandbox moves no real money and does not prompt real phones. To complete a deposit without Safaricom calling back, simulate the callback yourself:
```bash
docker compose exec db psql -U rembo rembo -c "select checkout_request_id, amount_kes, status from payments order by created_at desc limit 1"
curl -X POST http://localhost:8080/mpesa/callback/<MPESA_CALLBACK_SECRET from .env> -H 'content-type: application/json' -d '{"Body":{"stkCallback":{
 "MerchantRequestID":"x","CheckoutRequestID":"<ID FROM ABOVE>","ResultCode":0,"ResultDesc":"ok","CallbackMetadata":{"Item":[
 {"Name":"Amount","Value":200},{"Name":"MpesaReceiptNumber","Value":"TEST123456"},{"Name":"PhoneNumber","Value":254712345678}]}}}}'
```
Expect: the customer gets "Payment of KES 200 received", the owner gets "New booking", the booking is `confirmed`. Send the same curl again: nothing changes.
Real money needs Safaricom Go Live for the barber's till. Until then the real-callback path is covered by the automated tests only; say so in the demo.

## 3. WhatsApp
Needs a number that is allowed to message (see the account status in WhatsApp Manager). Callback URL = a public https URL (cloudflared/ngrok for a laptop):
`cloudflared tunnel --url http://localhost:8080`, set `PUBLIC_BASE_URL` to it, restart, paste `https://<url>/webhook` and `WA_VERIFY_TOKEN` in the Meta dashboard.
Repeat section 1 from WhatsApp. `DELIVERY FAILED ... reason` in the logs says why Meta refused a send.

## 4. What to send back
- the last 40 lines of `docker compose logs app` after each section, with phone numbers as printed (they are masked)
- the output of `scripts/doctor.py`
- which numbered step failed, and what the bot said instead
