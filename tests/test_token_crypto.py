import pytest
from app.token_crypto import encrypt_token, decrypt_token, valid_encryption_key, PREFIX

KEY = "a"*64  # 32 bytes hex, valid (distinct >=1 but our check only regex, so ok)
BAD_KEY = "short"

def test_valid_key():
    assert valid_encryption_key(KEY) is True
    assert valid_encryption_key("0123456789abcdef"*4) is True
    assert valid_encryption_key(BAD_KEY) is False
    assert valid_encryption_key("") is False
    assert valid_encryption_key(None) is False

def test_encrypt_decrypt():
    token = "EAA1234567890abcdef"
    enc = encrypt_token(token, KEY)
    assert enc.startswith(PREFIX)
    dec = decrypt_token(enc, KEY, require_encryption=True)
    assert dec == token

def test_encrypt_idempotent():
    token = "secret-token-123"
    enc1 = encrypt_token(token, KEY)
    enc2 = encrypt_token(enc1, KEY)
    assert enc1 == enc2

def test_decrypt_plaintext_without_require():
    assert decrypt_token("plain", KEY, require_encryption=False) == "plain"
    assert decrypt_token("", KEY, require_encryption=False) == ""
    assert decrypt_token(None, KEY, require_encryption=False) is None

def test_decrypt_plaintext_with_require_fails():
    try:
        decrypt_token("plain", KEY, require_encryption=True)
        assert False
    except RuntimeError as e:
        assert "plaintext" in str(e).lower()

def test_decrypt_wrong_key_fails():
    enc = encrypt_token("secret", KEY)
    other_key = "b"*64
    try:
        decrypt_token(enc, other_key, require_encryption=True)
        assert False
    except RuntimeError:
        pass

def test_empty_token():
    assert encrypt_token("", KEY) == ""
    assert encrypt_token(None, KEY) is None
