"""Integrate Telegram and Google Places through bounded server-side requests."""

import time

import httpx

from servicehub.config import settings
from servicehub.domain import RuleError
from servicehub.security import sign_payload, verify_payload


class ProviderError(RuntimeError):
    """Expose retry metadata without leaking request URLs or credentials."""

    def __init__(self, service: str, retry_after: int = 30, permanent: bool = False):
        """Capture a safe provider identifier and delivery policy."""
        super().__init__(service)
        self.retry_after = retry_after
        self.permanent = permanent


def telegram(method: str, payload: dict) -> dict:
    """Call Telegram without logging secret-bearing endpoint URLs."""
    token = settings().telegram_bot_token
    if not token:
        raise ProviderError("telegram_unconfigured", permanent=True)
    try:
        response = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("telegram_transport") from exc
    if not data.get("ok"):
        if "message is not modified" in data.get("description", ""):
            return {}
        raise ProviderError("telegram_rejected", data.get("parameters", {}).get("retry_after", 30), data.get("error_code") in {400, 401, 403})
    return data["result"]


def require_member(user_id: int) -> None:
    """Fail closed if the actor is not currently a member of the configured channel."""
    if not settings().telegram_channel_id:
        raise RuleError("Channel membership verification is not configured")
    member = telegram("getChatMember", {"chat_id": settings().telegram_channel_id, "user_id": user_id})
    if member.get("status") not in {"creator", "administrator", "member"}:
        raise RuleError("Join the ServiceHub channel before making a new commitment")


def places_request(path: str, *, body: dict | None = None, fields: str = "") -> dict:
    """Keep Google credentials server-side and return bounded provider results."""
    key = settings().google_places_api_key
    if not key:
        raise RuleError("Address search is unavailable; please try again later")
    headers = {"X-Goog-Api-Key": key}
    if fields:
        headers["X-Goog-FieldMask"] = fields
    try:
        response = httpx.request("POST" if body else "GET", f"https://places.googleapis.com/v1/{path}", json=body, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("places_unavailable") from exc


def autocomplete(query: str, session_token: str) -> list[dict]:
    """Return Canadian address suggestions with their provider attribution context."""
    result = places_request("places:autocomplete", body={"input": query, "includedRegionCodes": ["ca"], "sessionToken": session_token})
    return [{"id": row["placePrediction"]["placeId"], "text": row["placePrediction"]["text"]["text"]} for row in result.get("suggestions", []) if "placePrediction" in row]


def resolve_place(place_id: str, actor: int) -> dict:
    """Validate Ontario address components and sign an actor-bound selection token."""
    if not place_id.replace("-", "").replace("_", "").isalnum():
        raise RuleError("Invalid place identifier")
    result = places_request(f"places/{place_id}", fields="id,formattedAddress,addressComponents,location")
    components: dict[str, dict] = {}
    for component in result.get("addressComponents", []):
        for kind in component.get("types", []):
            components[kind] = component
    if components.get("country", {}).get("shortText") != "CA" or components.get("administrative_area_level_1", {}).get("shortText") != "ON":
        raise RuleError("Pickup and destination must both be in Ontario")
    city = components.get("locality", components.get("administrative_area_level_3", {})).get("longText", "Ontario")
    point = result.get("location", {})
    address = {"place_id": place_id, "exact": result.get("formattedAddress", ""), "street": components.get("route", {}).get("longText", ""), "city": city, "latitude": point["latitude"], "longitude": point["longitude"]}
    token = sign_payload({"kind": "address", "actor": actor, "address": address, "exp": time.time() + 1800}, settings().session_secret)
    return {"token": token, "label": address["exact"]}


def address_from_token(token: str, actor: int) -> dict:
    """Prevent browsers and agents from inventing verified locations or public address parts."""
    data = verify_payload(token, settings().session_secret)
    if data.get("kind") != "address" or data.get("actor") != actor:
        raise RuleError("Address selection does not belong to you")
    return data["address"]

