"""Provide deterministic database, identity, and network-isolation fixtures for backend tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Iterator
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.update(
    {
        "ENVIRONMENT": "local",
        "DATABASE_URL": "sqlite:///:memory:",
        "TELEGRAM_BOT_TOKEN": "test-telegram-token",
        "TELEGRAM_BOT_USERNAME": "servicehub_test_bot",
        "TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_CHANNEL_URL": "https://t.me/servicehub_test",
        "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
        "PUBLIC_URL": "https://servicehub.test",
        "SESSION_SECRET": "test-session-secret-that-is-longer-than-thirty-two-characters",
        "OPENAI_API_KEY": "",
        "GOOGLE_PLACES_API_KEY": "test-google-key",
        "TASK_MODE": "local",
        "RUNTIME_ROLE": "api",
    }
)

from servicehub.agents import runtime as agent_runtime  # noqa: E402
from servicehub.api import app as api_module  # noqa: E402
from servicehub.core.config import settings  # noqa: E402
from servicehub.database import session as session_module  # noqa: E402
from servicehub.database.session import Base  # noqa: E402
from servicehub.database.tables import Command, Job, Location, Offer, Ride, User  # noqa: E402
from servicehub.integrations import providers  # noqa: E402
from servicehub.integrations.telegram import handlers, messaging  # noqa: E402
from servicehub.security.tokens import sign_payload  # noqa: E402
from servicehub.workers import jobs  # noqa: E402

NOW = 1_800_000_000.0
SESSION_SECRET = os.environ["SESSION_SECRET"]


@pytest.fixture(autouse=True)
def isolate_settings_and_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear cached configuration and fail any unmocked outbound HTTP request."""

    settings.cache_clear()

    def blocked(*_args: object, **_kwargs: object) -> None:
        """Reject accidental provider traffic from otherwise offline tests."""

        raise AssertionError("An external network call escaped the test boundary")

    monkeypatch.setattr(providers.httpx, "post", blocked)
    monkeypatch.setattr(providers.httpx, "request", blocked)
    monkeypatch.setattr(agent_runtime, "client", blocked)
    monkeypatch.setattr(messaging, "client", blocked)
    monkeypatch.setattr(handlers, "telegram", blocked)
    monkeypatch.setattr(jobs, "telegram", blocked)
    yield
    settings.cache_clear()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[OrmSession]:
    """Create a fresh shared-connection SQLite schema and replace all session factories."""

    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(session_module, "Session", factory)
    monkeypatch.setattr(api_module, "Session", factory)
    monkeypatch.setattr(handlers, "Session", factory)
    monkeypatch.setattr(jobs, "Session", factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(db: OrmSession) -> Iterator[TestClient]:
    """Expose the FastAPI application against the per-test database."""

    del db
    with TestClient(api_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def user_factory(db: OrmSession) -> Callable[..., User]:
    """Create Telegram users with controllable commitment and assistant state."""

    def create(user_id: int = 101, name: str = "Rider", **values: object) -> User:
        """Persist one user and return its attached model."""

        defaults = {"id": user_id, "name": name, "active_ride": None, "mode": "supervisor", "context": {}}
        defaults.update(values)
        user = User(**defaults)
        db.add(user)
        db.flush()
        return user

    return create


@pytest.fixture
def address_factory() -> Callable[..., dict]:
    """Build a verified Ontario address with separate public and exact components."""

    def create(
        place_id: str = "place-pickup",
        street: str = "Sherrington Drive",
        city: str = "Thunder Bay",
        exact: str = "Unit 4, 10 Sherrington Drive, Thunder Bay, ON",
        latitude: float = 48.4000,
        longitude: float = -89.2500,
    ) -> dict:
        """Return an address payload in the provider contract used by drafts."""

        return {
            "place_id": place_id,
            "street": street,
            "city": city,
            "exact": exact,
            "latitude": latitude,
            "longitude": longitude,
        }

    return create


@pytest.fixture
def ride_factory(
    db: OrmSession,
    user_factory: Callable[..., User],
    address_factory: Callable[..., dict],
) -> Callable[..., Ride]:
    """Create rides with realistic private address details and configurable lifecycle fields."""

    def create(rider_id: int = 101, **values: object) -> Ride:
        """Persist a ride while creating its rider when absent."""

        if db.get(User, rider_id) is None:
            user_factory(rider_id, "Rider")
        pickup = address_factory()
        destination = address_factory(
            "place-destination",
            "Arthur Street West",
            exact="200 Arthur Street West, Thunder Bay, ON",
            latitude=48.3800,
            longitude=-89.2800,
        )
        defaults = {
            "rider_id": rider_id,
            "driver_id": None,
            "state": "Draft",
            "revision": 1,
            "generation": 1,
            "details": {
                "pickup": pickup,
                "destination": destination,
                "training_city": "Thunder Bay",
                "unit": "4B",
                "instructions": "Meet in the lobby",
            },
            "scheduled_at": None,
            "timezone": "America/Toronto",
            "price_cents": None,
            "created_at": NOW,
        }
        defaults.update(values)
        ride = Ride(**defaults)
        db.add(ride)
        db.flush()
        return ride

    return create


@pytest.fixture
def offer_factory(db: OrmSession, user_factory: Callable[..., User]) -> Callable[..., Offer]:
    """Create a driver quote bound to a ride generation."""

    def create(ride: Ride, driver_id: int = 202, price_cents: int = 2500, **values: object) -> Offer:
        """Persist a pending offer while ensuring the driver exists."""

        if db.get(User, driver_id) is None:
            user_factory(driver_id, "Driver")
        defaults = {
            "ride_id": ride.id,
            "driver_id": driver_id,
            "generation": ride.generation,
            "price_cents": price_cents,
            "revision": 1,
            "status": "pending",
        }
        defaults.update(values)
        offer = Offer(**defaults)
        db.add(offer)
        db.flush()
        return offer

    return create


@pytest.fixture
def command_factory(db: OrmSession, user_factory: Callable[..., User]) -> Callable[..., Command]:
    """Create actor-bound confirmation records with deterministic expiry and arguments."""

    def create(actor_id: int = 101, action: str = "cancel", **values: object) -> Command:
        """Persist one pending command while ensuring its actor exists."""

        if db.get(User, actor_id) is None:
            user_factory(actor_id, "Rider")
        defaults = {
            "actor_id": actor_id,
            "action": action,
            "args": {},
            "expires_at": NOW + 300,
            "created_at": NOW,
        }
        defaults.update(values)
        command = Command(**defaults)
        db.add(command)
        db.flush()
        return command

    return create


@pytest.fixture
def session_token() -> Callable[..., str]:
    """Sign bearer-session claims for API authentication tests."""

    def create(actor: int = 101, *, expires_at: float | None = None, kind: str = "session") -> str:
        """Return a signed token with an adjustable actor, expiry, and kind."""

        return sign_payload(
            {"kind": kind, "actor": actor, "exp": expires_at or time.time() + 3600},
            SESSION_SECRET,
        )

    return create


@pytest.fixture
def auth_headers(session_token: Callable[..., str]) -> Callable[..., dict[str, str]]:
    """Build the Authorization header expected by authenticated API routes."""

    def create(actor: int = 101, **token_values: object) -> dict[str, str]:
        """Return a bearer header for one signed user session."""

        return {"Authorization": f"Bearer {session_token(actor, **token_values)}"}

    return create


@pytest.fixture
def address_token() -> Callable[[int, dict], str]:
    """Sign resolved address payloads for actor-bound draft tests."""

    def create(actor: int, address: dict) -> str:
        """Return a current address token for one authenticated actor."""

        return sign_payload(
            {"kind": "address", "actor": actor, "address": address, "exp": time.time() + 600},
            SESSION_SECRET,
        )

    return create


@pytest.fixture
def telegram_init_data() -> Callable[..., str]:
    """Construct Telegram Mini App initialization data with a valid bot-token signature."""

    def create(
        user_id: int = 101,
        name: str = "Rider",
        *,
        auth_date: int | None = None,
        token: str = os.environ["TELEGRAM_BOT_TOKEN"],
        extra: list[tuple[str, str]] | None = None,
    ) -> str:
        """Return URL-encoded identity fields signed according to Telegram's Web App scheme."""

        fields = [
            ("auth_date", str(auth_date or int(time.time()))),
            ("query_id", "AAE-test-query"),
            ("user", json.dumps({"id": user_id, "first_name": name}, separators=(",", ":"))),
        ]
        fields.extend(extra or [])
        data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        signature = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        return urlencode([*fields, ("hash", signature)])

    return create


@pytest.fixture
def mysql_session() -> Iterator[OrmSession]:
    """Use only an explicitly named disposable MySQL test schema and clean it between cases."""

    database_url = os.getenv("SERVICEHUB_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("SERVICEHUB_TEST_MYSQL_URL is not configured")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail("SERVICEHUB_TEST_MYSQL_URL database name must end with '_test'")
    engine = create_engine(database_url, pool_pre_ping=True, isolation_level="READ COMMITTED")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    def clean() -> None:
        """Delete all rows while temporarily disabling MySQL foreign-key checks."""

        with engine.begin() as connection:
            connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
            connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    clean()
    session = factory()
    try:
        yield session
    finally:
        session.close()
        clean()
        engine.dispose()


@pytest.fixture
def job_factory(db: OrmSession) -> Callable[..., Job]:
    """Create durable work records for lease and retry tests."""

    def create(kind: str = "cleanup", **values: object) -> Job:
        """Persist a pending job with deterministic timing defaults."""

        defaults = {
            "dedupe": f"test-job-{time.time_ns()}",
            "kind": kind,
            "payload": {},
            "due_at": NOW - 1,
            "lease_until": 0,
            "attempts": 0,
            "status": "pending",
        }
        defaults.update(values)
        job = Job(**defaults)
        db.add(job)
        db.flush()
        return job

    return create


@pytest.fixture
def location_factory(db: OrmSession) -> Callable[..., Location]:
    """Create ordered location samples for proximity and training checks."""

    def create(ride: Ride, user_id: int, **values: object) -> Location:
        """Persist one GPS fix with valid accuracy and timing defaults."""

        defaults = {
            "ride_id": ride.id,
            "user_id": user_id,
            "latitude": 48.4000,
            "longitude": -89.2500,
            "accuracy": 15.0,
            "sampled_at": NOW,
            "source": "miniapp",
        }
        defaults.update(values)
        location = Location(**defaults)
        db.add(location)
        db.flush()
        return location

    return create
