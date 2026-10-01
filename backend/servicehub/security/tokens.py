"""Authenticate Telegram clients and issue short-lived signed Mini App sessions."""

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import parse_qsl


def telegram_identity(init_data: str, bot_token: str, now: float | None = None) -> dict:
    """Validate Telegram's signed initialization data and reject expired or duplicate fields."""

    now = time.time() if now is None else now
    pairs = parse_qsl(init_data, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs) or not bot_token:
        raise ValueError("Invalid Telegram initialization")
    supplied = data.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    age = now - int(data.get("auth_date", "0"))
    if not hmac.compare_digest(expected, supplied) or not -30 <= age <= 300:
        raise ValueError("Invalid or expired Telegram initialization")
    user = json.loads(data["user"])
    if not isinstance(user.get("id"), int) or user["id"] <= 0:
        raise ValueError("Invalid Telegram user")
    return user

def sign_payload(payload: dict, secret: str) -> str:
    """Create an authenticated token without exposing the signing secret."""

    if len(secret) < 32:
        raise ValueError("Session signing is not configured")
    body = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"

def verify_payload(token: str, secret: str) -> dict:
    """Reject modified, expired, and unconfigured application tokens."""

    body, signature = token.rsplit(".", 1)
    if len(secret) < 32 or not hmac.compare_digest(hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest(), signature):
        raise ValueError("Invalid session")
    data = json.loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if data.get("exp", 0) < time.time():
        raise ValueError("Expired session")
    return data
