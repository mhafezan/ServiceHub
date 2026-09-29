"""Project domain data into role-specific views without leaking exact addresses."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from servicehub.domain import Rides, RuleError, public_address
from servicehub.models import Offer, Ride, User


def ride_view(service: Rides, actor: int, ride: Ride) -> dict:
    """Expose exact routes only to the owner and currently selected driver."""
    privileged = actor in {ride.rider_id, ride.driver_id}
    details = ride.details
    result = {
        "id": ride.id, "state": ride.state, "revision": ride.revision, "generation": ride.generation,
        "pickup": public_address(details["pickup"]), "destination": public_address(details["destination"]),
        "scheduled_at": ride.scheduled_at, "timezone": ride.timezone,
        "scheduled_label": datetime.fromtimestamp(ride.scheduled_at, ZoneInfo(ride.timezone)).strftime("%Y-%m-%d %H:%M %Z") if ride.scheduled_at else "Immediate",
        "bid_until": ride.bid_until, "choose_until": ride.choose_until,
        "price_cents": ride.price_cents, "role": "rider" if actor == ride.rider_id else "driver" if actor == ride.driver_id else "observer",
        "pickup_attempt": ride.pickup_attempt if privileged else None,
        "server_time": service.now,
    }
    if privileged:
        result["exact_pickup"] = details["pickup"].get("exact", "")
        result["exact_destination"] = details["destination"].get("exact", "")
        result["instructions"] = details.get("instructions", "")
        result["unit"] = details.get("unit", "")
    quotes = service.db.scalars(select(Offer).where(Offer.ride_id == ride.id, Offer.generation == ride.generation)).all()
    result["offers"] = [
        {"id": quote.id, "revision": quote.revision, "price_cents": quote.price_cents, "status": quote.status,
         "driver_name": service.db.get(User, quote.driver_id).name}
        for quote in quotes if actor == ride.rider_id or actor == quote.driver_id
    ]
    return result


def visible_ride(service: Rides, actor: int, ride_id: str) -> dict:
    """Permit public-route inspection for bidding while enforcing private closed-ride access."""
    ride = service.db.get(Ride, ride_id)
    if not ride or (actor not in {ride.rider_id, ride.driver_id} and ride.state != "Open"):
        raise RuleError("Ride unavailable")
    return ride_view(service, actor, ride)


def command_summary(service: Rides, actor: int, action: str, args: dict) -> str:
    """Build immutable confirmation facts from owned resources, never model-provided descriptions."""
    ride = service.db.get(Ride, args.get("ride_id", ""))
    if not ride:
        raise RuleError("Ride not found")
    if action in {"publish", "accept", "reopen", "pickup"} and actor != ride.rider_id:
        raise RuleError("This is a rider-only action")
    if action in {"depart", "start", "complete"} and actor != ride.driver_id:
        raise RuleError("This is a driver-only action")
    if action == "cancel" and actor not in {ride.rider_id, ride.driver_id}:
        raise RuleError("This is not your ride")
    view = ride_view(service, actor, ride)
    text = f"{action.replace('_', ' ').title()}: {view['pickup']} → {view['destination']} · {view['scheduled_label']}"
    args["revision"] = ride.revision
    args["generation"] = ride.generation
    if action == "accept":
        quote = service.db.get(Offer, args.get("offer_id", ""))
        if not quote or quote.ride_id != ride.id:
            raise RuleError("Offer not found")
        if args.get("offer_revision") != quote.revision:
            raise RuleError("Offer changed; refresh before accepting")
        args["price_cents"] = quote.price_cents
        name = service.db.get(User, quote.driver_id).name
        text += f" · {name} · CAD {quote.price_cents / 100:.2f}"
    elif action == "offer":
        price = args.get("price_cents")
        if type(price) is not int or not 1 <= price <= 10_000_000:
            raise RuleError("Enter a valid positive CAD price")
        text += f" · CAD {price / 100:.2f}"
    elif action == "pickup":
        args["attempt"] = ride.pickup_attempt
        text += " · Confirm pickup occurred" if args.get("accepted") is True else " · Reject pickup"
    return text

