"""Verify ServiceHub HTTP contracts, authentication, privacy, and durable webhook ingress."""

import time

import pytest
from sqlalchemy import select

from servicehub.api import app as api_module
from servicehub.core.config import settings
from servicehub.database.tables import Job, User
from servicehub.integrations.providers import ProviderError


def test_health_and_readiness(client):
    """Expose successful liveness and database-backed readiness responses."""

    assert client.get("/health").json() == {"service": "ServiceHub", "status": "ok"}
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_fails_when_database_is_unavailable(client, monkeypatch):
    """Return an unhealthy server response when the readiness query cannot run."""

    class BrokenSession:
        """Raise on context entry to simulate an unavailable database."""

        def __enter__(self):
            """Fail before any query is issued."""

            raise RuntimeError("database unavailable")

        def __exit__(self, *_args):
            """Leave the failed context without suppressing errors."""

            return False

    monkeypatch.setattr(api_module, "Session", BrokenSession)
    assert client.get("/ready").status_code == 500


def test_session_exchange_creates_user_and_returns_bearer(client, db, telegram_init_data):
    """Exchange valid Telegram initialization data and persist a minimal user profile."""

    response = client.post("/api/session", json={"init_data": telegram_init_data(707, "Taylor")})
    assert response.status_code == 200
    token = response.json()["token"]
    assert client.get("/api/rides/current", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    db.expire_all()
    assert db.get(User, 707).name == "Taylor"


@pytest.mark.parametrize("payload", [{"init_data": "bad"}, {}, {"init_data": "x" * 8193}])
def test_session_exchange_rejects_invalid_envelopes(client, payload):
    """Reject malformed Telegram data and request bodies outside declared bounds."""

    assert client.post("/api/session", json=payload).status_code in {401, 422}


def test_bearer_authentication_rejects_missing_expired_modified_and_wrong_kind(
    client, session_token
):
    """Require a current, intact session-kind token on protected endpoints."""

    assert client.get("/api/rides/current").status_code == 401
    expired = session_token(expires_at=time.time() - 1)
    wrong_kind = session_token(kind="address")
    valid = session_token()
    modified = valid[:-1] + ("0" if valid[-1] != "0" else "1")
    for token in (expired, wrong_kind, modified):
        response = client.get("/api/rides/current", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


def test_place_search_bounds_success_and_safe_provider_failure(
    client, user_factory, auth_headers, monkeypatch
):
    """Validate address-search bounds and map provider failures without leaking details."""

    user_factory()
    monkeypatch.setattr(
        api_module,
        "autocomplete",
        lambda query, session_token: [{"id": session_token, "text": query}],
    )
    db_headers = auth_headers()
    response = client.get("/api/places?query=Main&session_token=abc", headers=db_headers)
    assert response.json() == [{"id": "abc", "text": "Main"}]
    assert client.get("/api/places?query=ab&session_token=abc", headers=db_headers).status_code == 422

    def unavailable(*_args):
        """Raise a provider error containing details that the API must hide."""

        raise ProviderError("places", detail="secret upstream response")

    monkeypatch.setattr(api_module, "autocomplete", unavailable)
    response = client.get("/api/places?query=Main&session_token=abc", headers=db_headers)
    assert response.status_code == 503
    assert "secret" not in response.text


def test_place_resolution_uses_authenticated_actor(client, user_factory, auth_headers, monkeypatch):
    """Pass the authenticated actor to the provider-bound address resolution boundary."""

    user_factory()
    seen = {}

    def resolve(place_id, actor):
        """Capture provider arguments and return its normal signed-selection shape."""

        seen.update(place_id=place_id, actor=actor)
        return {"token": "signed", "label": "10 Main Street"}

    monkeypatch.setattr(api_module, "resolve_place", resolve)
    response = client.get("/api/places/place-1", headers=auth_headers())
    assert response.json()["token"] == "signed"
    assert seen == {"place_id": "place-1", "actor": 101}


def test_draft_accepts_actor_bound_addresses_and_preserves_private_fields(
    client, db, user_factory, address_factory, address_token, auth_headers
):
    """Persist only verified address selections while retaining private unit and instructions."""

    user_factory()
    pickup = address_factory()
    destination = address_factory(
        place_id="destination",
        street="Arthur Street West",
        exact="200 Arthur Street West, Thunder Bay, ON",
    )
    response = client.post(
        "/api/rides/draft",
        headers=auth_headers(),
        json={
            "pickup_token": address_token(101, pickup),
            "destination_token": address_token(101, destination),
            "training_city": " Thunder Bay ",
            "unit": "4B",
            "instructions": "Use the side entrance",
        },
    )
    assert response.status_code == 200, response.text
    ride_id = response.json()["id"]
    db.expire_all()
    ride = db.get(api_module.Ride, ride_id)
    assert ride.details["training_city"] == "Thunder Bay"
    assert ride.details["unit"] == "4B"
    assert ride.details["instructions"] == "Use the side entrance"


def test_draft_rejects_wrong_actor_token_bad_schedule_and_input_bounds(
    client, user_factory, address_factory, address_token, auth_headers
):
    """Reject forged address ownership, invalid schedules, and oversized private input."""

    user_factory()
    address = address_factory()
    base = {
        "pickup_token": address_token(999, address),
        "destination_token": address_token(101, address),
        "training_city": "Thunder Bay",
    }
    assert client.post("/api/rides/draft", headers=auth_headers(), json=base).status_code == 409
    base["pickup_token"] = address_token(101, address)
    base.update(local_time="2020-01-01T12:00", timezone="America/Toronto")
    assert client.post("/api/rides/draft", headers=auth_headers(), json=base).status_code == 409
    base.pop("local_time")
    base["instructions"] = "x" * 501
    assert client.post("/api/rides/draft", headers=auth_headers(), json=base).status_code == 422


def test_current_and_specific_ride_visibility(client, db, user_factory, ride_factory, auth_headers):
    """Expose active and open rides according to participant and observer visibility rules."""

    rider = user_factory()
    user_factory(202, "Driver")
    user_factory(303, "Observer")
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=time.time() + 300,
        choose_until=time.time() + 600,
    )
    rider.active_ride = ride.id
    db.commit()
    current = client.get("/api/rides/current", headers=auth_headers(rider.id))
    observer = client.get(f"/api/rides/{ride.id}", headers=auth_headers(303))
    assert current.json()["role"] == "rider"
    assert observer.json()["role"] == "observer"
    assert "exact_pickup" not in observer.json()
    ride.state = "Completed"
    db.commit()
    assert client.get(f"/api/rides/{ride.id}", headers=auth_headers(303)).status_code == 409


def test_command_proposal_confirmation_membership_idempotency_and_kick(
    client, db, user_factory, ride_factory, auth_headers, monkeypatch
):
    """Require membership for commitments and execute an owned explicit confirmation once."""

    rider = user_factory()
    ride = ride_factory(rider_id=rider.id)
    db.commit()
    checks = []
    kicks = []
    monkeypatch.setattr(api_module, "require_member", lambda actor: checks.append(actor))
    monkeypatch.setattr(api_module, "kick", lambda: kicks.append(True))
    proposal = client.post(
        "/api/commands",
        headers=auth_headers(rider.id),
        json={"action": "publish", "args": {"ride_id": ride.id}},
    )
    assert proposal.status_code == 200, proposal.text
    command_id = proposal.json()["id"]
    first = client.post(f"/api/commands/{command_id}/confirm", headers=auth_headers(rider.id))
    second = client.post(f"/api/commands/{command_id}/confirm", headers=auth_headers(rider.id))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert checks == [rider.id]
    assert kicks == [True]
    assert client.post(f"/api/commands/{command_id}/confirm", headers=auth_headers(303)).status_code == 409


def test_location_publication_and_counterpart_lookup(
    client, db, user_factory, ride_factory, auth_headers
):
    """Accept participant fixes and return only the other participant's latest location."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    user_factory(303, "Observer")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route")
    db.commit()
    sampled_at = time.time()
    response = client.post(
        f"/api/rides/{ride.id}/location",
        headers=auth_headers(driver.id),
        json={"latitude": 48.4, "longitude": -89.25, "accuracy": 10, "sampled_at": sampled_at},
    )
    assert response.json() == {"ok": True}
    result = client.get(f"/api/rides/{ride.id}/location", headers=auth_headers(rider.id))
    assert result.json()["latitude"] == 48.4
    assert client.get(f"/api/rides/{ride.id}/location", headers=auth_headers(303)).status_code == 409


def test_webhook_validates_secret_size_identifier_and_deduplicates(
    client, db, monkeypatch
):
    """Persist each authenticated Telegram update once before acknowledging it."""

    kicks = []
    monkeypatch.setattr(api_module, "kick", lambda: kicks.append(True))
    endpoint = "/telegram/webhook"
    assert client.post(endpoint, json={"update_id": 1}).status_code == 403
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
    assert client.post(endpoint, headers=headers, json={"message": {}}).status_code == 422
    assert client.post(endpoint, headers=headers, content=b"x" * 128_001).status_code == 413
    payload = {"update_id": 17, "message": {"text": "/start"}}
    assert client.post(endpoint, headers=headers, json=payload).status_code == 200
    assert client.post(endpoint, headers=headers, json=payload).status_code == 200
    db.expire_all()
    jobs = db.scalars(select(Job).where(Job.kind == "telegram_update")).all()
    assert len(jobs) == 1
    assert jobs[0].dedupe == "telegram:17"
    assert len(kicks) == 2


def test_internal_worker_identity_accepts_only_configured_verified_account(
    client, monkeypatch
):
    """Authorize worker routes only for the configured verified Google service account."""

    import google.oauth2.id_token

    from servicehub.workers import jobs

    monkeypatch.setenv("WORKER_SERVICE_ACCOUNT", "worker@project.iam.gserviceaccount.com")
    monkeypatch.setenv("INTERNAL_AUDIENCE", "https://worker.test")
    settings.cache_clear()
    dispatched = []
    monkeypatch.setattr(jobs, "dispatch", lambda: dispatched.append(True))
    monkeypatch.setattr(
        google.oauth2.id_token,
        "verify_oauth2_token",
        lambda *_args: {"email": "worker@project.iam.gserviceaccount.com", "email_verified": True},
    )
    headers = {"Authorization": "Bearer oidc-token"}
    assert client.post("/internal/sweep", headers=headers).status_code == 200
    monkeypatch.setattr(
        google.oauth2.id_token,
        "verify_oauth2_token",
        lambda *_args: {"email": "attacker@example.com", "email_verified": True},
    )
    assert client.post("/internal/sweep", headers=headers).status_code == 403
    assert dispatched == [True]


def test_rule_and_provider_error_contracts_do_not_leak_details(
    client, user_factory, auth_headers, monkeypatch
):
    """Return stable domain and provider status shapes without upstream diagnostic text."""

    user_factory()
    missing = client.get("/api/rides/unknown", headers=auth_headers())
    assert missing.status_code == 409
    assert missing.json() == {"detail": "Ride unavailable"}

    def fail(*_args):
        """Simulate an upstream response containing sensitive provider diagnostics."""

        raise ProviderError("google", detail="API key rejected: top-secret")

    monkeypatch.setattr(api_module, "resolve_place", fail)
    provider = client.get("/api/places/valid-id", headers=auth_headers())
    assert provider.status_code == 503
    assert provider.json() == {"detail": "External service temporarily unavailable; please retry."}
    assert "top-secret" not in provider.text
