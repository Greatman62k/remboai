"""The keyword fallback: dates, service words, picks and cancellations. These run whenever the model is unavailable, so a wrong guess here
books the wrong day or cancels a booking. Today in this file is Monday 2 Nov 2026 unless a test says otherwise."""
from datetime import date

import pytest

from app.ai.llm import rule_extract
from app.timeparse import parse_day

MON = date(2026, 11, 2)
WED = date(2026, 11, 4)
SERVICES = [
    {"id": "1", "name": "Fade", "aliases": ["kinyozi"]}, {"id": "2", "name": "Beard trim", "aliases": []},
    {"id": "3", "name": "Braids", "aliases": ["kusuka"]}, {"id": "4", "name": "Kids cut", "aliases": []},
    {"id": "5", "name": "Silk press", "aliases": []},
]


def book(text, today=MON, services=SERVICES, state="idle"):
    return rule_extract(text, today, services, state)


# -- "next week" must mean next week ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,today,want", [
    ("beard trim Friday next week at 3pm", MON, date(2026, 11, 13)),
    ("silk press Wednesday next week afternoon", MON, date(2026, 11, 11)),
    ("braids Jumanne wiki ijayo asubuhi", MON, date(2026, 11, 10)),
    ("naomba miadi ya fade Jumatatu wiki ijayo", MON, date(2026, 11, 9)),
    ("fade friday next week", WED, date(2026, 11, 13)),
    ("fade wednesday next week", WED, date(2026, 11, 11)),
    ("fade monday next week", WED, date(2026, 11, 9)),
    ("fade Ijumaa wiki ijayo", WED, date(2026, 11, 13)),
])
def test_weekday_with_next_week_lands_in_next_week(text, today, want):
    assert parse_day(text, today) == want
    assert book(text, today).date == want.isoformat()


@pytest.mark.parametrize("text,today,want", [
    ("friday", MON, date(2026, 11, 6)), ("Jumamosi", MON, date(2026, 11, 7)), ("kesho", MON, date(2026, 11, 3)),
    ("friday", WED, date(2026, 11, 6)), ("this friday", WED, date(2026, 11, 6)), ("monday", WED, date(2026, 11, 9)),
    ("Alhamisi hii", MON, date(2026, 11, 5)), ("day after tomorrow", MON, date(2026, 11, 4)),
])
def test_ordinary_weekday_and_relative_days_are_unchanged(text, today, want):
    assert parse_day(text, today) == want


# -- calendar dates ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("fade on 10 November", date(2026, 11, 10)), ("fade on 10th of nov", date(2026, 11, 10)), ("fade November 10", date(2026, 11, 10)),
    ("fade on 9 November", date(2026, 11, 9)), ("beard trim on the 5th", date(2026, 11, 5)), ("kids cut tarehe 6 asubuhi", date(2026, 11, 6)),
    ("fade tarehe 6 novemba", date(2026, 11, 6)), ("fade 6 Novemba", date(2026, 11, 6)), ("braids on Saturday the 7th, morning", date(2026, 11, 7)),
    ("fade on the 1st", date(2026, 12, 1)), ("fade 1 November", date(2027, 11, 1)), ("fade 15 Desemba", date(2026, 12, 15)),
])
def test_calendar_dates(text, want):
    assert parse_day(text, MON) == want


@pytest.mark.parametrize("text", [
    "the 2pm one", "2nd one", "I'll take the 2nd slot", "my 1st time", "may I book a fade", "I have 2 kids", "fade at 5 pm", "fade at 3pm",
    "fade 10:30", "book may 5pm", "31 november", "32 november", "fade 0 november", "the 3rd option", "KES 500", "saa 5",
])
def test_things_that_are_not_dates_stay_none(text):
    assert parse_day(text, MON) is None


# -- greetings are not booking windows --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["Hello! good morning", "good morning", "good evening!", "Good afternoon", "habari ya asubuhi", "hi, good morning"])
def test_a_time_of_day_greeting_is_a_greeting(text):
    assert book(text).intent == "greeting"


def test_a_greeting_does_not_hide_a_real_request():
    ex = book("good morning, fade tomorrow morning")
    assert (ex.intent, ex.service, ex.date, ex.window) == ("book", "Fade", "2026-11-03", "morning")
    ex = book("good morning I need a fade tomorrow")
    assert (ex.intent, ex.service, ex.window) == ("book", "Fade", None)
    assert book("fade this morning").window == "morning"


# -- cancel means cancel, nothing softer --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "cancel", "ghairi", "please cancel my appointment", "cancel my booking for tomorrow", "kughairi miadi yangu", "nimeshindwa kuja, nataka kughairi miadi yangu",
    "sitaki tena miadi yangu ya kesho", "remove my appointment", "delete my booking", "kansel", "nacancel", "naomba kufuta miadi",
])
def test_explicit_cancellations_cancel(text):
    assert book(text).intent == "cancel", text


@pytest.mark.parametrize("text", [
    "I'm running late, be there in 15", "nimechelewa kidogo, nakuja", "I can't come tomorrow can we move it to Friday?", "can I reschedule to Thursday?",
    "nitakuja siku nyingine", "nimebadili mipango", "change the time to 4pm", "won't make it today, sorry", "book another day please",
    "what if I cancel?", "how do I cancel", "if I cancel do I lose the deposit", "do you have a cancellation policy", "siwezi kuja leo",
])
def test_lateness_rescheduling_and_questions_do_not_cancel(text):
    assert book(text).intent != "cancel", text


# -- service words: typos, Swahili concept words, no false hits --------------------------------------------------------------------------
@pytest.mark.parametrize("text,service", [
    ("fadde tomorow 3pm", "Fade"), ("fadee tmrw 2pm pls", "Fade"), ("bearad trim friday", "Beard trim"), ("kids cutt saturday morning", "Kids cut"),
    ("silkpress thurs 2pm", "Silk press"), ("brades sat morning pls", "Braids"), ("nataka kusukwa nywele Jumamosi", "Braids"),
    ("Naomba kusukwa Jumamosi saa tatu asubuhi", "Braids"), ("ndevu tu kesho saa sita mchana", "Beard trim"),
    ("nahitaji kunyoa ndevu leo mchana", "Beard trim"), ("saa tano kesho nataka kunyoa ndevu", "Beard trim"),
    ("mtoto anataka kunyoa kesho jioni", "Kids cut"), ("mtoto wangu anataka kunyoa Jumamosi asubuhi", "Kids cut"),
    ("my son needs a haircut Saturday", "Kids cut"), ("beard Friday", "Beard trim"), ("braid Saturday", "Braids"),
])
def test_services_are_found_despite_typos_and_swahili_words(text, service):
    assert book(text).service == service, text


@pytest.mark.parametrize("text", [
    "I made it to the shop", "I was late last time", "what time do you open", "I want a haircut tomorrow", "nataka kunyoa", "fate decides", "bread tomorrow",
    "kids" if False else "thanks", "wait a second",
])
def test_ordinary_words_do_not_become_services(text):
    assert book(text).service is None, text


def test_an_exact_service_word_beats_a_concept_word():
    assert book("kids fade tomorrow").service == "Fade"
    assert book("beard and a fade friday").service == "Fade"


def test_concept_words_need_exactly_one_matching_service():
    two_beards = [*SERVICES, {"id": "6", "name": "Beard dye", "aliases": []}]
    assert book("ndevu kesho", services=two_beards).service is None
    no_beard = [s for s in SERVICES if s["name"] != "Beard trim"]
    assert book("ndevu kesho", services=no_beard).service is None


# -- picking an offered slot in words -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,choice", [
    ("second one", 2), ("yes the first", 1), ("ya kwanza", 1), ("sawa nimechukua ya kwanza", 1), ("the third", 3), ("ya tatu", 3), ("3rd", 3),
    ("I'll take the second", 2), ("ya pili tafadhali", 2), ("first one please", 1), ("the 2nd", 2),
])
def test_an_ordinal_picks_an_offered_slot(text, choice):
    ex = book(text, state="offered")
    assert (ex.intent, ex.choice) == ("choose_slot", choice), text


@pytest.mark.parametrize("text", ["give me a second", "wait a second", "first thing tomorrow", "second one on Friday", "saa tatu", "tatu asubuhi", "my first time"])
def test_words_that_merely_contain_an_ordinal_are_not_picks(text):
    assert book(text, state="offered").intent != "choose_slot", text


def test_ordinals_only_pick_when_slots_are_on_offer():
    assert book("second one", state="idle").intent != "choose_slot"
    assert book("ya kwanza", state="held").intent != "choose_slot"
