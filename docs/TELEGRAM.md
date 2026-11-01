# Telegram channel

The same receptionist, on Telegram: free text, tap buttons, voice notes, hairstyle photos, M-Pesa deposits and the owner commands.
Telegram bots need no business verification, which makes this the quickest way to test everything from a phone. A customer's Telegram id is
not a phone number, so the bot asks "which M-Pesa number?" before the deposit prompt (a shared contact works too).

## Create the bot (2 minutes)
1. In Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`.
2. Copy the token (looks like `123456789:AAH...`). The number before the colon is the **bot id**.
3. Put it in `.env`: `TELEGRAM_BOT_TOKEN=123456789:AAH...` (never commit it).
4. Link the bot to the salon (new salon, or the existing one: the script updates it):
   ```bash
   python scripts/seed_salon.py --name "Brian's Kinyozi" --telegram-bot-id 123456789 --stylist Brian \
     --service "Fade:500:45:kinyozi,cut" --till <HEAD_OFFICE>:<TILL> --owner 2547XXXXXXXX --deposit 200
   ```
   A salon can have a WhatsApp number and a Telegram bot at once (`--phone-number-id` too). Add `--manual-till` after `--till` when the till has no Daraja API: deposits then confirm on the owner's tap.
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
- WhatsApp stays the main channel for customers in Kenya; Telegram exists so the product can be tested and demonstrated independently of Meta's account review.
