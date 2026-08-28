"""Unit tests for the field-level encryption helpers."""
from app import crypto


def test_secret_roundtrip():
    token = crypto.encrypt_secret("sk-1234-secret")
    assert token and token != "sk-1234-secret"
    assert crypto.decrypt_secret(token) == "sk-1234-secret"


def test_secret_blank_is_stored_empty():
    assert crypto.encrypt_secret("") == ""
    assert crypto.encrypt_secret(None) == ""
    assert crypto.decrypt_secret("") == ""
    assert crypto.decrypt_secret(None) == ""


def test_secret_decrypt_corrupt_is_empty():
    assert crypto.decrypt_secret("not-a-valid-fernet-token") == ""


def test_json_roundtrip():
    payload = {"Authorization": "Bearer topsecret", "X-Tenant": "acme"}
    token = crypto.encrypt_json(payload)
    assert token and "Bearer" not in token
    assert crypto.decrypt_json(token) == payload


def test_json_blank_or_corrupt_is_empty_dict():
    assert crypto.decrypt_json("") == {}
    assert crypto.decrypt_json(None) == {}
    assert crypto.decrypt_json("garbage") == {}


def test_encrypt_is_non_deterministic_but_decrypts_same():
    # Fernet is non-deterministic (random IV) but round-trips regardless.
    a = crypto.encrypt_secret("value")
    b = crypto.encrypt_secret("value")
    assert a != b
    assert crypto.decrypt_secret(a) == crypto.decrypt_secret(b) == "value"