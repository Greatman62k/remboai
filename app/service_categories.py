from __future__ import annotations

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("braids", "Braids & protective styles"),
    ("hair", "Hair styling & treatments"),
    ("barbering", "Barbering & haircuts"),
    ("nails", "Nails"),
    ("makeup", "Makeup & beauty"),
    ("other", "Other services"),
)

CATEGORY_LABELS = dict(CATEGORIES)
CATEGORY_CODES = frozenset(CATEGORY_LABELS)

def category_label(value: str | None) -> str:
    return CATEGORY_LABELS.get(value or "", CATEGORY_LABELS["other"])
