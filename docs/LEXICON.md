# Vocabulary (`app/lexicon.json`)

The words the receptionist understands without a language model live in one data file, not in code: what counts as salon talk (the screen in front of
the model), the keyword rules used when the model is unavailable, day and time words, greetings, and Swahili/Sheng prompt-injection phrases.
Adding words never means editing Python.

## Filling it from another tool
1. Give the tool `app/lexicon.json` and the prompt below. Save what it returns as `filled.json` (anywhere, not in the repo yet).
2. `python scripts/check_lexicon.py filled.json` prints counts per category, every entry that would be dropped (and why), and which example messages the
   word lists get wrong. Nothing is replaced. Fix and repeat.
3. `python scripts/check_lexicon.py filled.json --write app/lexicon.json` checks it and saves a cleaned copy: words that are too generic for their list
   (people slang like "dem" or "baba", "am"/"pm" as parts of the day, money slang as price words) are removed automatically.
4. Have a Kenyan Swahili or Sheng speaker skim the `sheng` lists: a wrong word makes unrelated chat reach the paid model or mis-read a date.
5. `python -m pytest -q`, commit.

How the lists are used: a first message goes to the paid model only if it has a word from `services_styles` or `booking_words`, an item on the salon's menu,
or (when the free keyword rules cannot read it) a specific price, payment, hours or cancel word. Prices, hours, cancel, greetings and bare days or times are
answered by the free rules. Request words ("nataka", "nifanyie") alone never qualify.

Entries: lowercase, 2-40 characters, letters digits spaces `'` `-` `.` only. A trailing `*` accepts any ending (`nyoa*` matches `nyoaji`), a trailing `+` a repeated last
letter (`hi+` matches `hiii`). Common function words (`na`, `ya`, `the`, `is`...) are dropped from the topic lists because they would let every message through.
Unusable entries are reported and ignored, never fatal.

## Prompt
> I am building a WhatsApp/Telegram receptionist for barbershops and salons in Nairobi. Customers write in English, Swahili and Sheng (Nairobi street Swahili-English mix),
> often with spelling shortcuts. Below is a JSON file of word lists. Fill in the `sw` and `sheng` lists (and add to `en` where Kenyans use English words differently) with words
> and short phrases real customers type. Rules: keep the exact same JSON structure and key names, return ONLY the JSON; lowercase; 2-40 characters; letters, digits, spaces,
> apostrophe, hyphen only; no regular expressions; end an entry with `*` when any ending is fine (`nyoa*`); never remove existing entries; never add everyday function words
> (na, ya, ni, kwa, the, and, is). Categories:
> - `services_styles`: this covers BOTH barbershops and women's salons. Barber: haircuts, fades, tapers, line-ups, shaves, beard work, kids' cuts. Salon: braids (box, knotless, cornrows, twists), weaves, wig making and installs, silk press, relaxers, natural hair care, treatments, colour, lashes, brows, makeup, waxing, nails (manicure, pedicure, gel, acrylic), facials. Spa: massage, facials, steam, body treatments. Also the words for the shop, the barber and the stylist, and slang for each. Aim for 80+ in `sw` and `sheng` together, covering all three kinds of business.
> - `booking_words`, `request_words`: how people ask for a slot ("nipangie", "niko free", "nataka kuja"), ask whether someone is free, or walk in.
> - `price_payment_words`, `hours_cancel_words`, `cancel`, `price`, `hours`: asking cost, deposits, M-Pesa and till, opening times, cancelling or moving a booking.
> - `sw_markers`: words that show someone is writing Swahili or Sheng.
> - `greetings`, `how_are_you`: Sheng and Swahili greetings and "how are you".
> - `today`, `tomorrow`, `day_after_tomorrow`, weekdays, `morning`, `afternoon`, `evening`: including Sheng and short forms. Careful: "kesho kutwa" is the DAY AFTER tomorrow.
> - `injection`: ways someone would tell an AI in Swahili or Sheng to ignore its rules, reveal its hidden instructions or change role. Only phrases no genuine customer would write.
> - `examples.on_topic`: 40 realistic customer messages (English, Swahili, Sheng, some with typos; mix barbershop, women's salon, nails and spa requests). `examples.off_topic`: 40 realistic messages with nothing to do with a salon (chat, questions, jokes, requests for code or money, attempts to manipulate an AI).
> After the JSON, list separately any entries you are unsure about.

