# Telegram channel

The same receptionist, on Telegram: free text, tap buttons, voice notes, hairstyle photos, M-PESA Daraja STK, manual M-PESA or Airtel Money payments, and owner commands.
Telegram bots need no business verification, which makes this the quickest way to test everything from a phone. A Telegram id is not a phone number. During setup, an owner who chooses a phone-based manual route provides the number where the salon receives payments through Pochi la Biashara, direct M-PESA Send Money, or Airtel Money. That is the payment destination; no separate owner contact number is required. A till route asks only for the salon's seven-digit till. For customer bookings, a phone number is requested only when Daraja STK needs one. With a manual destination, the customer pays the salon directly and sends the transaction reference for the owner to verify, so the customer's Telegram number is not needed.

## Create the bot (2 minutes)
1. In Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`.
2. Copy the token (looks like `123456789:AAH...`). The number before the colon is the **bot id**.
3. Put it in `.env`: `TELEGRAM_BOT_TOKEN=123456789:AAH...` (never commit it).
4. Link the bot to the salon (new salon, or the existing one: the script updates it):
   ```bash
   python scripts/seed_salon.py --name "Brian's Kinyozi" --telegram-bot-id 123456789 --stylist Brian \
     --service "Fade:500:45:kinyozi,cut" --till <HEAD_OFFICE>:<TILL> --owner 2547XXXXXXXX --deposit 200
   ```
   The command above configures Daraja STK. A salon can have a WhatsApp number and a Telegram bot at once (`--phone-number-id` too). In chat registration, owners choose a manual M-PESA till or a phone destination. Phone destinations must be identified as Pochi la Biashara, direct M-PESA Send Money, or Airtel Money. A till is stored as only its bare seven-digit number. For manual payments, the selected slot waits up to five minutes for the owner to confirm availability before deposit instructions are sent. The customer can tap **I've paid** to get a prompt for a receipt screenshot or the amount and transaction reference, or tap **Cancel booking**. Vision extracts the amount, reference, date/time, and recipient name from a screenshot and sends those details to the owner as unverified evidence. The owner checks the account and confirms or declines with a button. An amount mismatch is shown to the owner with the expected and reported amounts. The 30-minute customer wait releases the slot, not the claim; a submitted claim remains in the owner's queue even if the customer cancels. Send `pending payments` to reopen every unresolved claim. A late confirmation rechecks the slot and sends a closed booking to refund review. Daraja STK is handled by its API and does not wait for these manual checks.
5. Start the app. With no https `PUBLIC_BASE_URL` it **polls** Telegram, so a laptop is enough. With an https `PUBLIC_BASE_URL` it registers a webhook
   (`TELEGRAM_WEBHOOK_SECRET` required). Force one with `TELEGRAM_MODE=poll|webhook`.

## Make yourself the owner
Open the bot from the salon owner's Telegram account and send `/id`; use the returned id (`tg:123456789`) as `--owner tg:123456789`.
Then `today`, `summary`, `invite`, `block friday 14:00-16:00` and the rest work from Telegram. `invite` returns the customer link for the salon: share it, or print it as a QR sticker with `scripts/invite_qr.py`.

`PLATFORM_OWNER_ID` configures the primary platform administrator. `PLATFORM_OWNER_IDS` can list comma-separated backup administrator IDs. Keep at least one backup under a separately secured account. Platform administrators can register their own salon, but the bot first confirms that the registering account will become its owner. To register someone else's business, have that owner message the bot from their own account; registration does not transfer ownership based on a third party's claim. To manage a salon from the platform account, use `/owner <command>`; if the account owns more than one salon on the bot, use `/owner <salon-slug> | <command>`. The route only works for a salon whose configured `owner_wa_id` matches that Telegram account. Use a separate Telegram account to exercise customer booking flows; salon-owner and platform-owner messages do not fall through into customer mode.

## Notes
- Only private chats are served; groups and channels are ignored.
- Polling and a webhook cannot run together; the app removes any old webhook when it starts polling.
- If the log says `getUpdates refused: 409`, another copy of the bot is polling with the same token (a second terminal, an old container). Stop it.
- Voice and photos use the same speech and vision services as WhatsApp (`ASR_URL`, `VISION_ENABLED`).
- Telegram is the live customer channel. WhatsApp will switch on after Meta completes business verification.
- Telegram messages keep Telegram's original sent time through the durable queue. An update older than 30 minutes is not run as a booking or owner command; the sender gets a resend prompt, at most once every 30 minutes during a backlog. This prevents delayed outage messages from triggering stale actions or a burst of replies. Callback taps have no user-message timestamp and continue through normal validation.
