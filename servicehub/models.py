"""Define durable bookings, commands, location samples, and transactional delivery records."""

import time
import uuid

from sqlalchemy import JSON, BigInteger, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from servicehub.db import Base

def uid() -> str:
    """Create an opaque identifier safe for callbacks and client references."""
    return uuid.uuid4().hex

class User(Base):
    """Serialize each user's commitments and retain minimal Telegram identity."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128))
    active_ride: Mapped[str | None] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16), default="supervisor")
    context: Mapped[dict] = mapped_column(JSON, default=dict)

class Ride(Base):
    """Persist a versioned ride and all server-authoritative booking deadlines."""
    __tablename__ = "rides"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    rider_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(40), default="Draft", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    details: Mapped[dict] = mapped_column(JSON)
    scheduled_at: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Toronto")
    bid_until: Mapped[float | None] = mapped_column(Float, index=True)
    choose_until: Mapped[float | None] = mapped_column(Float)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    pickup_attempt: Mapped[str | None] = mapped_column(String(32))
    pickup_until: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float)
    ended_at: Mapped[float | None] = mapped_column(Float, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    channel_message: Mapped[int | None] = mapped_column(BigInteger)

class Offer(Base):
    """Keep one revisioned quote per driver, ride, and reopening generation."""
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("ride_id", "driver_id", "generation"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    ride_id: Mapped[str] = mapped_column(ForeignKey("rides.id"), index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")

class Command(Base):
    """Bind a confirmed action to its authenticated actor and exact arguments."""
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    args: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

class Job(Base):
    """Serve as durable inbox, outbox, and scheduled work with expiring leases."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    dedupe: Mapped[str] = mapped_column(String(190), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    due_at: Mapped[float] = mapped_column(Float, index=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

class Event(Base):
    """Record privacy-minimal state transitions and operator interventions."""
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    ride_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(40))
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)

class Location(Base):
    """Retain consented first-party GPS fixes independently of address provider data."""
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    ride_id: Mapped[str] = mapped_column(ForeignKey("rides.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    accuracy: Mapped[float | None] = mapped_column(Float)
    sampled_at: Mapped[float] = mapped_column(Float, index=True)
    source: Mapped[str] = mapped_column(String(16))

class TrainingSample(Base):
    """Store de-identified completed-ride features with rolling annual expiry."""
    __tablename__ = "ride_training_samples"
    completion_ref: Mapped[str] = mapped_column(String(32), primary_key=True)
    distance_metres: Mapped[int | None] = mapped_column(Integer)
    agreed_price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    pickup_city: Mapped[str] = mapped_column(String(100))
    completed_at: Mapped[float] = mapped_column(Float, index=True)
    distance_source: Mapped[str] = mapped_column(String(32), default="driver_gps")
    coverage: Mapped[float] = mapped_column(Float)
    quality_flags: Mapped[list] = mapped_column(JSON)
    eligible: Mapped[bool]

class Template(Base):
    """Cache validated LLM-generated wording without user-specific values."""
    __tablename__ = "templates"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
