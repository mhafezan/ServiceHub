"""Verify Telegram identity validation and application-token integrity boundaries."""

import time

import pytest
from conftest import SESSION_SECRET

from servicehub.security.tokens import sign_payload, telegram_identity, verify_payload


def test_telegram_identity_accepts_current_valid_initialization(telegram_init_data):
    """Extract the authenticated Telegram user from correctly signed current data."""

    init_data = telegram_init_data(321, "Ava")
    assert telegram_identity(init_data, "test-telegram-token")["id"] == 321


@pytest.mark.parametrize(
    "init_data",
    ["", "auth_date=bad&hash=nope", "user=%7B%7D&auth_date=1&hash=nope"],
)
def test_telegram_identity_rejects_malformed_data(init_data):
    """Reject incomplete, malformed, and incorrectly signed Telegram envelopes."""

    with pytest.raises((ValueError, KeyError)):
        telegram_identity(init_data, "test-telegram-token")


def test_telegram_identity_rejects_expired_duplicate_and_invalid_user(telegram_init_data):
    """Reject replayed, ambiguous, and non-positive Telegram identities."""

    now = int(time.time())
    with pytest.raises(ValueError, match="expired"):
        telegram_identity(
            telegram_init_data(auth_date=now - 301), "test-telegram-token", now=float(now)
        )
    with pytest.raises(ValueError, match="Invalid Telegram initialization"):
        telegram_identity(
            telegram_init_data(extra=[("query_id", "duplicate")]),
            "test-telegram-token",
        )
    with pytest.raises(ValueError, match="Invalid Telegram user"):
        telegram_identity(telegram_init_data(user_id=-1), "test-telegram-token")


def test_signed_payload_round_trip_and_tamper_detection():
    """Return intact claims while rejecting modified bodies, signatures, and expired claims."""

    token = sign_payload({"kind": "session", "actor": 101, "exp": time.time() + 60}, SESSION_SECRET)
    assert verify_payload(token, SESSION_SECRET)["actor"] == 101
    body, signature = token.split(".")
    with pytest.raises(ValueError, match="Invalid session"):
        verify_payload(body + "A." + signature, SESSION_SECRET)
    with pytest.raises(ValueError, match="Invalid session"):
        verify_payload(body + "." + "0" * len(signature), SESSION_SECRET)
    expired = sign_payload({"exp": time.time() - 1}, SESSION_SECRET)
    with pytest.raises(ValueError, match="Expired"):
        verify_payload(expired, SESSION_SECRET)
    with pytest.raises(ValueError, match="configured"):
        sign_payload({"exp": time.time() + 1}, "short")
