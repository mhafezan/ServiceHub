"""Enforce ride transitions, race-safe commitments, proximity, and training-data quality."""

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from servicehub.database.tables import Command, Event, Job, Location, Offer, Ride, TrainingSample, User, uid

TERMINAL = {"Completed", "Cancelled", "Expired", "Administrative_Closure"}
TRACKABLE = {"Driver_En_Route", "Pickup_Confirmation_Pending", "Trip_Started"}
ACTIONS = {"publish", "offer", "withdraw", "accept", "depart", "start", "pickup", "complete", "cancel", "reopen"}

class RuleError(ValueError):
    """Report a recoverable business-rule rejection to the caller."""

def enqueue(db: Session, kind: str, payload: dict, due: float, dedupe: str | None = None) -> Job:
    """Append work within the caller's transaction, deduplicating known business events."""

    key = dedupe or uid()
    existing = db.scalar(select(Job).where(Job.dedupe == key))
    if existing:
        return existing
    job = Job(kind=kind, payload=payload, due_at=due, dedupe=key)
    db.add(job)
    return job

def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Calculate great-circle distance using coordinates supplied by the caller."""

    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))

def scheduled_timestamp(local_time: str | None, timezone: str, now: float) -> float | None:
    """Validate Ontario local pickup times, including DST folds and gaps."""

    if not local_time:
        return None
    if timezone not in {"America/Toronto", "America/Winnipeg", "America/Atikokan"}:
        raise RuleError("Select an Ontario timezone")
    try:
        naive = datetime.fromisoformat(local_time)
        if naive.tzinfo:
            raise RuleError("Supply a local date and time without an offset")
        zone = ZoneInfo(timezone)
        candidates = {
            naive.replace(tzinfo=zone, fold=fold).timestamp()
            for fold in (0, 1)
            if datetime.fromtimestamp(naive.replace(tzinfo=zone, fold=fold).timestamp(), zone).replace(tzinfo=None) == naive
        }
        if len(candidates) != 1:
            raise RuleError("This local time is ambiguous or nonexistent; choose another time")
        result = candidates.pop()
    except (ValueError, KeyError) as exc:
        raise RuleError(str(exc)) from exc
    if not now + 1800 <= result <= now + 30 * 86400:
        raise RuleError("Scheduled pickup must be between 30 minutes and 30 days ahead")
    return result

def public_address(address: dict) -> str:
    """Build a public label from trusted route/locality components only, never formatted addresses."""

    city = address.get("city", "Ontario")
    street = address.get("street", "")
    return f"{street}, {city}" if street else city

class Rides:
    """Execute all ride operations in a caller-owned database transaction."""

    def __init__(self, db: Session, now: float | None = None):
        """Bind transaction and clock for deterministic rules and tests."""
        self.db = db
        self.now = time.time() if now is None else now

    def users(self, *ids: int) -> dict[int, User]:
        """Acquire user locks in stable order before any ride or offer locks."""
        rows = self.db.scalars(select(User).where(User.id.in_(sorted(set(ids)))).order_by(User.id).with_for_update()).all()
        if len(rows) != len(set(ids)):
            raise RuleError("Start the bot before using this service")
        return {row.id: row for row in rows}

    def ride(self, ride_id: str) -> Ride:
        """Lock and refresh a ride after relevant participant locks are held."""
        ride = self.db.scalar(select(Ride).where(Ride.id == ride_id).execution_options(populate_existing=True).with_for_update())
        if not ride:
            raise RuleError("Ride not found")
        return ride

    def notify(self, ride: Ride, action: str, actor: int) -> None:
        """Record a state change and enqueue version-aware notifications atomically."""
        ride.revision += 1
        self.db.add(Event(ride_id=ride.id, actor_id=actor, action=action))
        enqueue(self.db, "ride_event", {"ride_id": ride.id, "revision": ride.revision, "action": action}, self.now)

    def draft(self, actor: int, details: dict, scheduled_at: float | None, timezone: str) -> Ride:
        """Save the user's draft from server-verified Ontario address data."""
        self.users(actor)
        existing = self.db.scalar(select(Ride).where(Ride.rider_id == actor, Ride.state == "Draft").with_for_update())
        if existing:
            existing.details, existing.scheduled_at, existing.timezone = details, scheduled_at, timezone
            existing.revision += 1
            return existing
        ride = Ride(rider_id=actor, details=details, scheduled_at=scheduled_at, timezone=timezone)
        self.db.add(ride)
        self.db.flush()
        return ride

    def propose(self, actor: int, action: str, args: dict) -> Command:
        """Prepare a user-confirmable command without performing its business mutation."""
        if action not in ACTIONS:
            raise RuleError("Unsupported action")
        self.users(actor)
        command = Command(actor_id=actor, action=action, args=args, expires_at=self.now + 300)
        self.db.add(command)
        self.db.flush()
        return command

    def confirm(self, actor: int, command_id: str) -> dict:
        """Execute only the authenticated user's unexpired command, once."""
        snapshot = self.db.get(Command, command_id)
        if not snapshot or snapshot.actor_id != actor:
            raise RuleError("Confirmation not found")
        args = snapshot.args
        snapshot_ride = self.db.get(Ride, args.get("ride_id", ""))
        participants = {actor}
        if snapshot_ride:
            participants.add(snapshot_ride.rider_id)
            if snapshot_ride.driver_id:
                participants.add(snapshot_ride.driver_id)
        if snapshot.action == "accept":
            offer = self.db.get(Offer, args.get("offer_id", ""))
            if offer:
                participants.add(offer.driver_id)
        users = self.users(*participants)
        command = self.db.scalar(select(Command).where(Command.id == command_id).with_for_update().execution_options(populate_existing=True))
        if command.result is not None:
            return command.result
        if command.expires_at < self.now:
            raise RuleError("Confirmation expired; please try again")
        ride = self.ride(args.get("ride_id", ""))
        if snapshot_ride and ride.driver_id and ride.driver_id not in users:
            raise RuleError("Assignment changed; refresh the ride")
        if command.action != "offer" and args.get("revision") != ride.revision:
            raise RuleError("Ride changed; refresh before confirming")
        result = self.perform(actor, command.action, args, ride, users)
        command.result = result
        self.db.flush()
        return result

    def perform(self, actor: int, action: str, args: dict, ride: Ride, users: dict[int, User]) -> dict:
        """Dispatch confirmed commands through explicit state and ownership checks."""
        if action == "publish":
            self.publish(actor, ride, users[actor])
        elif action == "offer":
            return self.offer(actor, ride, users[actor], args)
        elif action == "accept":
            self.accept(actor, ride, args, users)
        elif action == "withdraw":
            quote = self.db.scalar(select(Offer).where(Offer.id == args.get("offer_id")).with_for_update())
            if not quote or quote.ride_id != ride.id or quote.driver_id != actor or quote.status != "pending":
                raise RuleError("Offer is not available for withdrawal")
            quote.status = "withdrawn"
            self.notify(ride, "offer_withdrawn", actor)
        elif action == "reopen":
            if actor != ride.rider_id or ride.state != "Awaiting_Rider":
                raise RuleError("Only the rider can reopen this request")
            if ride.scheduled_at and ride.scheduled_at < self.now + 600:
                raise RuleError("Cancel and create a request with a new pickup time")
            ride.generation += 1
            self.open_window(ride)
            self.notify(ride, "reopened", actor)
        elif action == "cancel":
            self.cancel(actor, ride, users)
        elif action == "depart":
            self.require_driver(actor, ride, {"Matched"})
            ride.state = "Driver_En_Route"
            self.notify(ride, "departed", actor)
        elif action == "start":
            self.require_driver(actor, ride, {"Driver_En_Route"})
            if ride.scheduled_at and self.now < ride.scheduled_at - 900:
                raise RuleError("Pickup confirmation opens 15 minutes before scheduled pickup")
            self.near(ride, "pickup")
            ride.state = "Pickup_Confirmation_Pending"
            ride.pickup_attempt, ride.pickup_until = uid(), self.now + 300
            enqueue(self.db, "pickup_expiry", {"ride_id": ride.id, "attempt": ride.pickup_attempt}, ride.pickup_until)
            self.notify(ride, "pickup_requested", actor)
        elif action == "pickup":
            if actor != ride.rider_id or ride.state != "Pickup_Confirmation_Pending":
                raise RuleError("No pickup confirmation is pending for you")
            if args.get("attempt") != ride.pickup_attempt or self.now > (ride.pickup_until or 0):
                raise RuleError("Pickup confirmation expired")
            if args.get("accepted") is True:
                self.near(ride, "pickup")
                ride.state, ride.started_at = "Trip_Started", self.now
            else:
                ride.state = "Driver_En_Route"
            ride.pickup_attempt = None
            self.notify(ride, "pickup_confirmed" if ride.state == "Trip_Started" else "pickup_rejected", actor)
        elif action == "complete":
            self.require_driver(actor, ride, {"Trip_Started"})
            self.near(ride, "destination")
            ride.state, ride.ended_at = "Completed", self.now
            for user in users.values():
                if user.active_ride == ride.id:
                    user.active_ride = None
            self.training(ride)
            self.notify(ride, "completed", actor)
        else:
            raise RuleError("Unknown operation")
        return {"ride_id": ride.id, "state": ride.state, "revision": ride.revision}

    def publish(self, actor: int, ride: Ride, user: User) -> None:
        """Claim the rider's active slot and start the five-minute bidding period."""
        if actor != ride.rider_id or ride.state != "Draft":
            raise RuleError("Only your draft can be submitted")
        if user.active_ride:
            active = self.db.get(Ride, user.active_ride)
            remaining = max(0, math.ceil((active.bid_until or self.now) - self.now)) if active else 0
            raise RuleError(f"You already have an active request. Bidding lasts five minutes; {remaining} seconds remain. Open My Ride or cancel first.")
        if ride.scheduled_at and not self.now + 1800 <= ride.scheduled_at <= self.now + 30 * 86400:
            raise RuleError("Choose a scheduled pickup 30 minutes to 30 days ahead")
        user.active_ride = ride.id
        self.open_window(ride)
        self.notify(ride, "published", actor)

    def open_window(self, ride: Ride) -> None:
        """Start a generation-specific bidding and selection deadline."""
        ride.state = "Open"
        ride.bid_until, ride.choose_until = self.now + 300, self.now + 600
        enqueue(self.db, "expiry", {"ride_id": ride.id, "generation": ride.generation}, ride.bid_until)

    def offer(self, actor: int, ride: Ride, user: User, args: dict) -> dict:
        """Create or revise a quote only while the current bidding window is open."""
        if actor == ride.rider_id or user.active_ride:
            raise RuleError("You must be available and cannot offer on your own request")
        if ride.state != "Open" or self.now >= (ride.bid_until or 0) or args.get("generation") != ride.generation:
            raise RuleError("This bidding window has closed")
        price = args.get("price_cents")
        if type(price) is not int or not 1 <= price <= 10_000_000:
            raise RuleError("Enter a positive CAD amount with at most two decimal places (maximum CAD 100,000)")
        quote = self.db.scalar(select(Offer).where(Offer.ride_id == ride.id, Offer.driver_id == actor, Offer.generation == ride.generation).with_for_update())
        if quote:
            quote.price_cents, quote.revision, quote.status = price, quote.revision + 1, "pending"
        else:
            quote = Offer(ride_id=ride.id, driver_id=actor, generation=ride.generation, price_cents=price)
            self.db.add(quote)
        self.db.flush()
        enqueue(self.db, "offer_event", {"offer_id": quote.id, "revision": quote.revision}, self.now)
        return {"offer_id": quote.id, "revision": quote.revision, "state": "pending"}

    def accept(self, actor: int, ride: Ride, args: dict, users: dict[int, User]) -> None:
        """Assign a single available driver and invalidate all competing commitments atomically."""
        if actor != ride.rider_id or ride.state not in {"Open", "Selecting"} or self.now >= (ride.choose_until or 0):
            raise RuleError("This request is no longer accepting a selection")
        quote = self.db.scalar(select(Offer).where(Offer.id == args.get("offer_id")).with_for_update())
        if not quote or quote.ride_id != ride.id or quote.generation != ride.generation or quote.status != "pending":
            raise RuleError("Offer is no longer available")
        if quote.revision != args.get("offer_revision") or quote.price_cents != args.get("price_cents"):
            raise RuleError("Offer price changed; review the new amount")
        driver = users.get(quote.driver_id)
        if not driver or driver.active_ride:
            raise RuleError("Driver is no longer available")
        ride.driver_id, ride.price_cents, ride.state = driver.id, quote.price_cents, "Matched"
        driver.active_ride = ride.id
        all_quotes = self.db.scalars(select(Offer).where(Offer.status == "pending", (Offer.ride_id == ride.id) | (Offer.driver_id == driver.id)).order_by(Offer.id).with_for_update()).all()
        for other in all_quotes:
            other.status = "accepted" if other.id == quote.id else "closed"
            if other.id != quote.id:
                enqueue(self.db, "offer_event", {"offer_id": other.id, "revision": other.revision}, self.now)
        if ride.scheduled_at:
            for offset in (1800, 300):
                if ride.scheduled_at - offset > self.now:
                    enqueue(self.db, "reminder", {"ride_id": ride.id, "generation": ride.generation}, ride.scheduled_at - offset)
        self.notify(ride, "matched", actor)

    def cancel(self, actor: int, ride: Ride, users: dict[int, User]) -> None:
        """Cancel a request or return a driver-cancelled ride to rider-controlled reopening."""
        if actor not in {ride.rider_id, ride.driver_id} or ride.state in TERMINAL | {"Trip_Started"}:
            raise RuleError("This ride cannot be cancelled by you")
        old_driver = ride.driver_id
        if old_driver and old_driver in users:
            users[old_driver].active_ride = None
        for quote in self.db.scalars(select(Offer).where(Offer.ride_id == ride.id).with_for_update()):
            quote.status = "closed"
        if actor == ride.rider_id:
            ride.state, ride.ended_at = "Cancelled", self.now
            users[ride.rider_id].active_ride = None
        else:
            ride.state, ride.driver_id, ride.price_cents = "Awaiting_Rider", None, None
        ride.pickup_attempt = None
        if old_driver:
            enqueue(self.db, "direct", {"chat_id": old_driver, "key": "cancelled", "values": {}}, self.now)
        self.notify(ride, "cancelled", actor)

    def require_driver(self, actor: int, ride: Ride, states: set[str]) -> None:
        """Require the assigned driver and an explicitly permitted transition."""
        if actor != ride.driver_id or ride.state not in states:
            raise RuleError("This driver action is unavailable in the current state")

    def latest_location(self, ride: Ride, user_id: int) -> Location | None:
        """Read the latest consented sample for one ride participant."""
        return self.db.scalar(select(Location).where(Location.ride_id == ride.id, Location.user_id == user_id).order_by(Location.sampled_at.desc()).limit(1))

    def near(self, ride: Ride, endpoint: str) -> None:
        """Fail closed unless a fresh accurate driver fix is inside the endpoint radius."""
        fix = self.latest_location(ride, ride.driver_id or 0)
        if not fix or not 0 <= self.now - fix.sampled_at <= 60 or fix.accuracy is None or fix.accuracy > 100:
            raise RuleError("Share a fresh driver location with accuracy of 100 metres or better")
        address = ride.details[endpoint]
        if metres((fix.latitude, fix.longitude), (address["latitude"], address["longitude"])) > 500:
            raise RuleError(f"Driver must be within 500 metres of {endpoint}")

    def location(self, actor: int, ride_id: str, latitude: float, longitude: float, accuracy: float | None, sampled_at: float, source: str) -> None:
        """Accept current consented fixes only for active matched participants."""
        self.users(actor)
        ride = self.ride(ride_id)
        if actor not in {ride.rider_id, ride.driver_id} or ride.state not in TRACKABLE:
            raise RuleError("Location sharing is available after driver departure until trip completion")
        if not all(math.isfinite(value) for value in (latitude, longitude, sampled_at)):
            raise RuleError("Invalid location")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or not -10 <= self.now - sampled_at <= 60:
            raise RuleError("Location is invalid or stale")
        if accuracy is not None and (not math.isfinite(accuracy) or accuracy < 0):
            raise RuleError("Invalid location accuracy")
        previous = self.latest_location(ride, actor)
        if previous and sampled_at <= previous.sampled_at:
            return
        self.db.add(Location(ride_id=ride.id, user_id=actor, latitude=latitude, longitude=longitude, accuracy=accuracy, sampled_at=sampled_at, source=source))

    def training(self, ride: Ride) -> None:
        """Derive first-party trip distance and exclude incomplete or implausible tracks."""
        fixes = self.db.scalars(select(Location).where(Location.ride_id == ride.id, Location.user_id == ride.driver_id, Location.sampled_at >= (ride.started_at or 0), Location.sampled_at <= self.now).order_by(Location.sampled_at)).all()
        flags: set[str] = set()
        accepted = [fix for fix in fixes if fix.accuracy is not None and fix.accuracy <= 100]
        if len(accepted) != len(fixes):
            flags.add("poor_accuracy")
        if len(accepted) < 2:
            flags.add("insufficient_samples")
        distance, covered = 0.0, 0.0
        for previous, current in zip(accepted, accepted[1:]):
            seconds = current.sampled_at - previous.sampled_at
            segment = metres((previous.latitude, previous.longitude), (current.latitude, current.longitude))
            if seconds > 120:
                flags.add("tracking_gap")
            elif seconds <= 0 or segment / seconds > 60:
                flags.add("implausible_jump")
            else:
                distance += segment
                covered += seconds
        if not accepted or accepted[0].sampled_at - (ride.started_at or self.now) > 60 or self.now - accepted[-1].sampled_at > 60:
            flags.add("endpoint_coverage")
        duration = max(1, self.now - (ride.started_at or self.now))
        self.db.add(TrainingSample(completion_ref=ride.id, distance_metres=round(distance) if len(accepted) >= 2 else None, agreed_price_cents=ride.price_cents or 0, pickup_city=ride.details["training_city"], completed_at=self.now, coverage=min(1.0, covered / duration), quality_flags=sorted(flags), eligible=not flags))

    def expire(self, ride_id: str, generation: int) -> None:
        """Advance elapsed bidding windows without affecting later ride generations."""
        snapshot = self.db.get(Ride, ride_id)
        if not snapshot:
            return
        users = self.users(snapshot.rider_id)
        ride = self.ride(ride_id)
        if ride.generation != generation or ride.state not in {"Open", "Selecting"}:
            return
        if self.now < (ride.bid_until or 0):
            return
        available = self.db.scalar(select(Offer.id).where(Offer.ride_id == ride.id, Offer.generation == generation, Offer.status == "pending").limit(1))
        if available and self.now < (ride.choose_until or 0):
            ride.state = "Selecting"
            enqueue(self.db, "expiry", {"ride_id": ride.id, "generation": generation}, ride.choose_until or self.now, f"selection:{ride.id}:{generation}")
        else:
            ride.state, ride.ended_at = "Expired", self.now
            users[ride.rider_id].active_ride = None
            for offer in self.db.scalars(select(Offer).where(Offer.ride_id == ride.id, Offer.status == "pending").with_for_update()):
                offer.status = "expired"
        self.notify(ride, "expired" if ride.state == "Expired" else "selection", ride.rider_id)

    def cleanup(self) -> None:
        """Remove expired personal records and enforce rolling training-data retention."""
        self.db.execute(delete(TrainingSample).where(TrainingSample.completed_at <= self.now - 365 * 86400))
        finished = select(Ride.id).where(Ride.ended_at <= self.now - 86400)
        self.db.execute(delete(Location).where(Location.ride_id.in_(finished)))
        self.db.execute(delete(Location).where(Location.sampled_at <= self.now - 31 * 86400))
        for ride in self.db.scalars(select(Ride).where(Ride.ended_at <= self.now - 30 * 86400)):
            ride.details = {"pickup": {"city": "Ontario"}, "destination": {"city": "Ontario"}, "training_city": ""}
        for user in self.db.scalars(select(User)):
            if user.context.get("updated_at", 0) < self.now - 30 * 86400:
                user.context = {}
        self.db.execute(delete(Command).where(Command.created_at < self.now - 30 * 86400))
        self.db.execute(delete(Job).where(Job.status.in_(["done", "dead"]), Job.created_at < self.now - 30 * 86400))
        self.db.execute(delete(Event).where(Event.created_at < self.now - 90 * 86400))
