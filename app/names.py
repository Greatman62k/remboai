"""Business names on a shared bot: when two names are close enough that customers could mix the businesses up, and how a business is shown.

Customers reach a business by its invite link, never by typing a name, so similar names cannot misroute anyone. They still matter to a
customer reading "Welcome to Zuri Cuts" and to an owner who wants their bookings. The area ("Zuri Cuts (Westlands)") tells them apart."""
from __future__ import annotations

import difflib
import re
import unicodedata

from . import guard

# Words that say what a business is, not which one it is: "Baraka Barbershop" and "Baraka Salon" are the same name to a customer.
_GENERIC = {"the", "and", "barber", "barbers", "barbershop", "barbershops", "shop", "shops", "kinyozi", "salon", "salons", "saloon", "saloons",
            "beauty", "hair", "studio", "studios", "spa", "parlour", "parlor", "place", "centre", "center", "ltd", "limited"}
_NOT_AN_AREA = {"yes", "no", "ok", "okay", "none", "clear", "remove", "ndio", "hapana", "sawa", "futa", "ondoa"}


def _words(name: str) -> tuple[list[str], list[str]]:
    """(the words that identify the business, all the words), lower-cased, without accents, punctuation or possessives."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().casefold().replace("&", " and ")
    s = re.sub(r"'s\b", "", s)
    everything = re.sub(r"[^a-z0-9]+", " ", s).split()
    return [w for w in everything if w not in _GENERIC], everything


def name_key(name: str) -> str:
    """The part of a name that identifies the business: accents, case, punctuation, possessives, generic words and doubled letters removed."""
    core, everything = _words(name)
    return re.sub(r"(.)\1+", r"\1", " ".join(core or everything))


def similarity(a: str, b: str) -> float:
    """0 to 1, and 0 when the names are different businesses. Only the distinguishing part is compared, so a long shared word such as
    'Barbershop' cannot make 'Amani Barbershop' and 'Amina Barbershop' look alike. A name made only of generic words is compared as written."""
    ka, kb = name_key(a), name_key(b)
    if ka and ka == kb:
        return 1.0
    if not _words(a)[0] or not _words(b)[0]:                 # nothing but generic words ("Barber Shop"): there is no distinguishing part
        plain = difflib.SequenceMatcher(None, " ".join(_words(a)[1]), " ".join(_words(b)[1])).ratio()
        return plain if plain >= 0.8 else 0.0
    keyed = difflib.SequenceMatcher(None, ka, kb).ratio()
    return keyed if keyed >= 0.85 else 0.0


def similar(a: str, b: str) -> bool:
    return similarity(a, b) > 0


def clean_area(text: str | None) -> str | None:
    """An area or street to show customers beside the name. None when it is not usable: too short or long, a phone number, a link, or a yes/no."""
    area = " ".join(guard.clean(text or "").strip(" .,-").split())
    if not 2 <= len(area) <= 40 or area.casefold() in _NOT_AN_AREA:
        return None
    if re.search(r"https?://|www\.|@|\d{5,}|\.(com|co\.ke|ke|me|io)\b", area, re.I):
        return None
    return area


def display(name: str, area: str | None) -> str:
    area = (area or "").strip()
    return f"{name} ({area})" if area else name


def label_sql(prefix: str = "") -> str:
    """SQL for the name customers see, so every query that loads a business for a message shows the area the same way."""
    return f"(CASE WHEN NULLIF(btrim({prefix}area), '') IS NULL THEN {prefix}name ELSE {prefix}name || ' (' || btrim({prefix}area) || ')' END)"


LABEL = label_sql()
S_LABEL = label_sql("s.")
