"""Seed-script argument parsing: bad --till/--service values fail with usage, not a traceback."""
import pytest

from scripts.seed_salon import parse_service, parse_till


def test_parse_till():
    assert parse_till("0712345678:5012345") == ("till", "0712345678", "5012345")
    for bad in ("", "notill", "a:b:c", " :5012345", "0712345678:"):
        with pytest.raises(ValueError):
            parse_till(bad)


def test_parse_service():
    assert parse_service("Fade:500:45:kinyozi,cut") == ("Fade", 500, 45, ["kinyozi", "cut"])
    assert parse_service("Shave:300:20") == ("Shave", 300, 20, [])
    assert parse_service("Fade:500:45:") == ("Fade", 500, 45, [])
    for bad in ("", "Fade", "Fade:500", ":500:45", "Fade:much:45", "Fade:500:long"):
        with pytest.raises(ValueError):
            parse_service(bad)
