"""Verify Telegram, Google Places, membership, and signed-address provider boundaries."""

import time
from types import SimpleNamespace

import httpx
import pytest
from conftest import SESSION_SECRET

from servicehub.integrations import providers
from servicehub.integrations.providers import ProviderError
from servicehub.rides.domain import RuleError
from servicehub.security.tokens import sign_payload


class FakeResponse:
    """Provide the small HTTP response surface used by provider integrations."""

    def __init__(self, data=None, *, json_error=False, status_error=False):
        """Store a response body and optional JSON or status failure behavior."""

        self.data = data
        self.json_error = json_error
        self.status_error = status_error

    def json(self):
        """Return configured JSON or simulate invalid provider output."""

        if self.json_error:
            raise ValueError("invalid JSON")
        return self.data

    def raise_for_status(self):
        """Raise a transport-compatible status error when requested."""

        if self.status_error:
            request = httpx.Request("GET", "https://provider.test")
            raise httpx.HTTPStatusError("failed", request=request, response=httpx.Response(500))


def provider_settings(**values):
    """Return a complete minimal provider configuration namespace."""

    defaults = {
        "telegram_bot_token": "token",
        "telegram_channel_id": "-1001",
        "google_places_api_key": "google-key",
        "session_secret": SESSION_SECRET,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_telegram_success_and_message_not_modified(monkeypatch):
    """Return successful Telegram results and treat unchanged edits as idempotent success."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *_args, **_kwargs: FakeResponse({"ok": True, "result": {"message_id": 7}}),
    )
    assert providers.telegram("sendMessage", {"chat_id": 1}) == {"message_id": 7}
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
        ),
    )
    assert providers.telegram("editMessageText", {}) == {}


def test_telegram_missing_token_transport_and_invalid_json(monkeypatch):
    """Classify unconfigured, timeout, and malformed Telegram responses without leaking URLs."""

    monkeypatch.setattr(providers, "settings", lambda: provider_settings(telegram_bot_token=""))
    with pytest.raises(ProviderError) as missing:
        providers.telegram("sendMessage", {})
    assert missing.value.permanent is True
    monkeypatch.setattr(providers, "settings", provider_settings)

    def timeout(*_args, **_kwargs):
        """Raise a provider transport timeout."""

        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(providers.httpx, "post", timeout)
    with pytest.raises(ProviderError) as transport:
        providers.telegram("sendMessage", {})
    assert transport.value.permanent is False
    monkeypatch.setattr(
        providers.httpx, "post", lambda *_args, **_kwargs: FakeResponse(json_error=True)
    )
    with pytest.raises(ProviderError, match="telegram_transport"):
        providers.telegram("sendMessage", {})


@pytest.mark.parametrize(
    ("error_code", "permanent"),
    [(400, True), (401, True), (403, True), (429, False), (500, False)],
)
def test_telegram_rejection_retry_policy(monkeypatch, error_code, permanent):
    """Preserve Telegram retry-after values and classify permanent authorization failures."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            {
                "ok": False,
                "error_code": error_code,
                "description": "rejected",
                "parameters": {"retry_after": 17},
            }
        ),
    )
    with pytest.raises(ProviderError) as failure:
        providers.telegram("sendMessage", {})
    assert failure.value.retry_after == 17
    assert failure.value.permanent is permanent


@pytest.mark.parametrize("status", ["creator", "administrator", "member"])
def test_membership_accepts_channel_members(monkeypatch, status):
    """Allow the three Telegram states that represent current channel membership."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    monkeypatch.setattr(providers, "telegram", lambda *_args: {"status": status})
    providers.require_member(101)


def test_membership_rejects_nonmembers_and_missing_configuration(monkeypatch):
    """Fail closed for departed users and channels without a configured identifier."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    monkeypatch.setattr(providers, "telegram", lambda *_args: {"status": "left"})
    with pytest.raises(RuleError, match="Join"):
        providers.require_member(101)
    monkeypatch.setattr(providers, "settings", lambda: provider_settings(telegram_channel_id=""))
    with pytest.raises(RuleError, match="not configured"):
        providers.require_member(101)


def test_places_request_uses_key_mask_method_and_timeout(monkeypatch):
    """Keep the API key in headers and construct bounded GET and POST requests."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    calls = []

    def request(method, url, **kwargs):
        """Capture the outgoing Google Places request contract."""

        calls.append((method, url, kwargs))
        return FakeResponse({"ok": True})

    monkeypatch.setattr(providers.httpx, "request", request)
    assert providers.places_request("places/id", fields="id,location") == {"ok": True}
    providers.places_request("places:autocomplete", body={"input": "Main"})
    assert calls[0][0] == "GET" and calls[1][0] == "POST"
    assert calls[0][2]["headers"] == {
        "X-Goog-Api-Key": "google-key",
        "X-Goog-FieldMask": "id,location",
    }
    assert calls[1][2]["timeout"] == 10


def test_places_errors_and_canadian_autocomplete_filter(monkeypatch):
    """Convert provider failures and request Canadian-only address predictions."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    original_places_request = providers.places_request
    captured = {}

    def places(path, *, body=None, fields=""):
        """Capture autocomplete restrictions and return mixed suggestion kinds."""

        captured.update(path=path, body=body, fields=fields)
        return {
            "suggestions": [
                {"placePrediction": {"placeId": "one", "text": {"text": "10 Main"}}},
                {"queryPrediction": {"text": {"text": "ignored"}}},
            ]
        }

    monkeypatch.setattr(providers, "places_request", places)
    assert providers.autocomplete("Main", "session") == [{"id": "one", "text": "10 Main"}]
    assert captured["body"]["includedRegionCodes"] == ["ca"]
    monkeypatch.setattr(providers, "places_request", original_places_request)
    monkeypatch.setattr(providers, "settings", lambda: provider_settings(google_places_api_key=""))
    with pytest.raises(RuleError, match="unavailable"):
        providers.places_request("places/id")


def test_resolve_place_accepts_ontario_and_rejects_invalid_or_external(monkeypatch):
    """Sign normalized Ontario addresses while rejecting malformed IDs and other provinces."""

    monkeypatch.setattr(providers, "settings", provider_settings)

    def ontario(*_args, **_kwargs):
        """Return a representative Ontario Place Details response."""

        return {
            "formattedAddress": "10 Main Street, Thunder Bay, ON",
            "addressComponents": [
                {"longText": "Canada", "shortText": "CA", "types": ["country"]},
                {"longText": "Ontario", "shortText": "ON", "types": ["administrative_area_level_1"]},
                {"longText": "Thunder Bay", "types": ["locality"]},
                {"longText": "Main Street", "types": ["route"]},
            ],
            "location": {"latitude": 48.4, "longitude": -89.25},
        }

    monkeypatch.setattr(providers, "places_request", ontario)
    result = providers.resolve_place("place_1", 101)
    assert result["label"].startswith("10 Main")
    assert providers.address_from_token(result["token"], 101)["city"] == "Thunder Bay"
    with pytest.raises(RuleError, match="Invalid place"):
        providers.resolve_place("bad/id", 101)

    def quebec(*_args, **_kwargs):
        """Return a valid Canadian response outside Ontario."""

        data = ontario()
        data["addressComponents"][1]["shortText"] = "QC"
        return data

    monkeypatch.setattr(providers, "places_request", quebec)
    with pytest.raises(RuleError, match="Ontario"):
        providers.resolve_place("place_2", 101)


def test_address_tokens_bind_actor_and_detect_tampering(monkeypatch):
    """Reject address selections signed for another actor or modified after signing."""

    monkeypatch.setattr(providers, "settings", provider_settings)
    token = sign_payload(
        {"kind": "address", "actor": 101, "address": {"city": "Thunder Bay"}, "exp": time.time() + 60},
        SESSION_SECRET,
    )
    assert providers.address_from_token(token, 101)["city"] == "Thunder Bay"
    with pytest.raises(RuleError, match="belong"):
        providers.address_from_token(token, 202)
    with pytest.raises(ValueError, match="Invalid session"):
        providers.address_from_token(token[:-1] + "0", 101)
