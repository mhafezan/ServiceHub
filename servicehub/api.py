"""Expose authenticated Mini App and Telegram ingress with durable background processing."""

import hmac
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from servicehub.config import settings
from servicehub.db import Session
from servicehub.domain import Rides, RuleError, enqueue, scheduled_timestamp
from servicehub.models import Command, Job, Ride, User
from servicehub.providers import ProviderError, address_from_token, autocomplete, require_member, resolve_place
from servicehub.security import sign_payload, telegram_identity, verify_payload
from servicehub.views import command_summary, visible_ride

app = FastAPI(title="ServiceHub", docs_url="/api/docs" if settings().environment == "local" else None)


@app.exception_handler(RuleError)
async def rule_error(request: Request, exc: RuleError):
    """Expose expected domain rejections without stack traces or secrets."""
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ProviderError)
async def provider_error(request: Request, exc: ProviderError):
    """Return a safe outage response rather than provider internals."""
    return JSONResponse({"detail": "External service temporarily unavailable; please retry."}, status_code=503)


def actor(authorization: Annotated[str, Header()] = "") -> int:
    """Authenticate bearer sessions minted only from verified Telegram initialization."""
    try:
        data = verify_payload(authorization.removeprefix("Bearer "), settings().session_secret)
        if data.get("kind") != "session":
            raise ValueError("Wrong token kind")
        return int(data["actor"])
    except (ValueError, KeyError):
        raise HTTPException(401, "Open the Mini App from Telegram again") from None


Actor = Annotated[int, Depends(actor)]


class Init(BaseModel):
    """Accept only Telegram's signed initialization envelope."""
    init_data: str = Field(max_length=8192)


class DraftInput(BaseModel):
    """Collect independently supplied details and server-signed address selections."""
    pickup_token: str = Field(max_length=8192)
    destination_token: str = Field(max_length=8192)
    training_city: str = Field(min_length=2, max_length=100)
    unit: str = Field(default="", max_length=100)
    instructions: str = Field(default="", max_length=500)
    local_time: str | None = None
    timezone: str = "America/Toronto"


class ProposalInput(BaseModel):
    """Carry requested action arguments without client authority to execute them."""
    action: str = Field(max_length=32)
    args: dict


class LocationInput(BaseModel):
    """Validate explicitly consented foreground GPS submissions."""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    sampled_at: float


@app.get("/health")
def health():
    """Expose liveness without requiring external services."""
    return {"service": "ServiceHub", "status": "ok"}


@app.get("/ready")
def ready():
    """Verify database connectivity before routing deployment traffic."""
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/api/session")
def session(body: Init):
    """Exchange recent Telegram initialization for a short-lived actor-bound session."""
    try:
        identity = telegram_identity(body.init_data, settings().telegram_bot_token)
    except (ValueError, KeyError):
        raise HTTPException(401, "Invalid Telegram initialization") from None
    with Session.begin() as db:
        user = db.get(User, identity["id"])
        if not user:
            db.add(User(id=identity["id"], name=identity.get("first_name", "Member")[:128]))
    return {"token": sign_payload({"kind": "session", "actor": identity["id"], "exp": time.time() + 3600}, settings().session_secret)}


@app.get("/api/places")
def search_places(user_id: Actor, query: str, session_token: str):
    """Proxy a bounded address search without exposing the Google API key."""
    if not 3 <= len(query) <= 150 or len(session_token) > 64:
        raise HTTPException(422, "Invalid search")
    return autocomplete(query, session_token)


@app.get("/api/places/{place_id}")
def resolve_address(place_id: str, user_id: Actor):
    """Return an Ontario-validated selection token for this actor."""
    return resolve_place(place_id, user_id)


@app.post("/api/rides/draft")
def draft(body: DraftInput, user_id: Actor):
    """Save verified address selections and privately supplied ride details."""
    try:
        details = {"pickup": address_from_token(body.pickup_token, user_id), "destination": address_from_token(body.destination_token, user_id), "training_city": body.training_city.strip(), "unit": body.unit, "instructions": body.instructions}
        scheduled = scheduled_timestamp(body.local_time, body.timezone, time.time())
    except ValueError as exc:
        raise RuleError(str(exc)) from exc
    with Session.begin() as db:
        ride = Rides(db).draft(user_id, details, scheduled, body.timezone)
        return {"id": ride.id, "revision": ride.revision}


@app.get("/api/rides/current")
def current_ride(user_id: Actor):
    """Restore the caller's active commitment without exposing unrelated rides."""
    with Session() as db:
        user = db.get(User, user_id)
        return visible_ride(Rides(db), user_id, user.active_ride) if user and user.active_ride else None


@app.get("/api/rides/{ride_id}")
def get_ride(ride_id: str, user_id: Actor):
    """Retrieve a role-filtered ride view."""
    with Session() as db:
        return visible_ride(Rides(db), user_id, ride_id)


@app.post("/api/commands")
def propose(body: ProposalInput, user_id: Actor):
    """Prepare immutable confirmation facts for the browser to display."""
    with Session.begin() as db:
        service = Rides(db)
        summary = command_summary(service, user_id, body.action, body.args)
        command = service.propose(user_id, body.action, body.args)
        return {"id": command.id, "summary": summary}


def confirm_command(user_id: int, command_id: str) -> dict:
    """Share confirmation logic between Telegram callbacks and Mini App requests."""
    with Session() as db:
        command = db.get(Command, command_id)
        if not command or command.actor_id != user_id:
            raise RuleError("Confirmation not found")
        if command.result is not None:
            return command.result
        if command.action in {"publish", "offer", "accept"}:
            require_member(user_id)
    with Session.begin() as db:
        result = Rides(db).confirm(user_id, command_id)
    kick()
    return result


@app.post("/api/commands/{command_id}/confirm")
def confirm(command_id: str, user_id: Actor):
    """Commit only an explicit confirmation from its authenticated owner."""
    return confirm_command(user_id, command_id)


@app.post("/api/rides/{ride_id}/location")
def publish_location(ride_id: str, body: LocationInput, user_id: Actor):
    """Store first-party GPS fixes only during the caller's active tracking window."""
    with Session.begin() as db:
        Rides(db).location(user_id, ride_id, body.latitude, body.longitude, body.accuracy, body.sampled_at, "miniapp")
    return {"ok": True}


@app.get("/api/rides/{ride_id}/location")
def view_location(ride_id: str, user_id: Actor):
    """Return only the other participant's last fix while sharing is active."""
    from servicehub.domain import TRACKABLE

    with Session() as db:
        service = Rides(db)
        ride = db.get(Ride, ride_id)
        if not ride or user_id not in {ride.rider_id, ride.driver_id} or ride.state not in TRACKABLE:
            raise RuleError("Location access is unavailable")
        target = ride.driver_id if user_id == ride.rider_id else ride.rider_id
        fix = service.latest_location(ride, target)
        if not fix:
            return None
        return {"latitude": fix.latitude, "longitude": fix.longitude, "accuracy": fix.accuracy, "sampled_at": fix.sampled_at, "stale": service.now - fix.sampled_at > 60}


def kick() -> None:
    """Best-effort immediate dispatch; the durable scheduler sweep repairs failures."""
    if settings().task_mode == "gcp":
        try:
            from servicehub.worker import dispatch
            dispatch()
        except Exception:
            pass


@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: Annotated[str, Header()] = ""):
    """Persist authenticated Telegram events before acknowledging delivery."""
    secret = settings().telegram_webhook_secret
    if not secret or not hmac.compare_digest(secret, x_telegram_bot_api_secret_token):
        raise HTTPException(403, "Invalid webhook secret")
    raw = await request.body()
    if len(raw) > 128_000:
        raise HTTPException(413, "Update too large")
    update = await request.json()
    if type(update.get("update_id")) is not int:
        raise HTTPException(422, "Missing update identifier")
    try:
        with Session.begin() as db:
            enqueue(db, "telegram_update", update, time.time(), f"telegram:{update['update_id']}")
    except IntegrityError:
        pass
    kick()
    return {"ok": True}


def internal_identity(authorization: Annotated[str, Header()] = "") -> None:
    """Verify GCP OIDC identity even when the worker shares the application image."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    try:
        data = id_token.verify_oauth2_token(authorization.removeprefix("Bearer "), GoogleRequest(), settings().internal_audience)
        if data.get("email") != settings().worker_service_account or not data.get("email_verified"):
            raise ValueError("Wrong service account")
    except Exception:
        raise HTTPException(403, "Invalid worker identity") from None


@app.post("/internal/sweep", dependencies=[Depends(internal_identity)])
def sweep():
    """Repair pending dispatch and enqueue retention work on a trusted schedule."""
    from servicehub.worker import dispatch

    with Session.begin() as db:
        enqueue(db, "cleanup", {}, time.time(), f"cleanup:{int(time.time() // 86400)}")
    dispatch()
    return {"ok": True}


@app.post("/internal/jobs/{job_id}", dependencies=[Depends(internal_identity)])
def work(job_id: str):
    """Process a durable job through a leased worker."""
    from servicehub.worker import run_job

    run_job(job_id)
    return {"ok": True}


@app.get("/guides/ride")
def public_guide():
    """Publish the same ride guide used by the read-only information agent."""
    return FileResponse(Path(__file__).resolve().parents[1] / "Documentation" / "Ride-Service-Quick-Guide.md", media_type="text/plain")


static = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if static.exists():
    app.mount("/", StaticFiles(directory=static, html=True), name="miniapp")

