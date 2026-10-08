"""Verify ride invariants, transitions, location rules, training quality, and retention."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW
from sqlalchemy import select

from servicehub.database.tables import Command, Event, Job, Location, Offer, TrainingSample
from servicehub.rides.domain import Rides, RuleError, metres, public_address, scheduled_timestamp


def test_address_time_and_distance_helpers(address_factory):
    """Keep public addresses private and reject invalid scheduled-time edge cases."""

    address = address_factory()
    assert public_address(address) == "Sherrington Drive, Thunder Bay"
    assert "Unit" not in public_address(address)
    assert metres((48.4, -89.25), (48.4, -89.25)) == 0
    assert metres((48.4, -89.25), (48.401, -89.25)) == pytest.approx(111.2, abs=1)
    future = datetime.fromtimestamp(NOW + 3600, ZoneInfo("America/Toronto")).replace(tzinfo=None)
    assert scheduled_timestamp(future.isoformat(timespec="minutes"), "America/Toronto", NOW) == pytest.approx(
        NOW + 3600, abs=60
    )
    with pytest.raises(RuleError, match="Ontario timezone"):
        scheduled_timestamp(future.isoformat(), "UTC", NOW)
    with pytest.raises(RuleError, match="30 minutes"):
        scheduled_timestamp(
            datetime.fromtimestamp(NOW + 60, ZoneInfo("America/Toronto")).replace(tzinfo=None).isoformat(),
            "America/Toronto",
            NOW,
        )
    with pytest.raises(RuleError, match="ambiguous or nonexistent"):
        scheduled_timestamp("2026-03-08T02:30", "America/Toronto", 1_767_225_600)
    with pytest.raises(RuleError, match="ambiguous or nonexistent"):
        scheduled_timestamp("2026-11-01T01:30", "America/Toronto", 1_767_225_600)


def test_draft_updates_and_publish_claims_active_slot(db, user_factory, address_factory):
    """Update one draft and atomically open its five-minute bidding window."""

    rider = user_factory()
    service = Rides(db, NOW)
    details = {
        "pickup": address_factory(),
        "destination": address_factory(place_id="destination", street="Arthur Street West"),
        "training_city": "Thunder Bay",
    }
    ride = service.draft(rider.id, details, None, "America/Toronto")
    revised = service.draft(rider.id, {**details, "unit": "9"}, None, "America/Toronto")
    assert revised.id == ride.id
    assert revised.revision == 2
    service.publish(rider.id, ride, rider)
    db.flush()
    assert ride.state == "Open"
    assert ride.bid_until == NOW + 300
    assert ride.choose_until == NOW + 600
    assert rider.active_ride == ride.id
    assert db.scalar(select(Job).where(Job.kind == "expiry")) is not None
    assert db.scalar(select(Event).where(Event.action == "published")) is not None


def test_second_active_request_explains_remaining_window(db, user_factory, ride_factory):
    """Explain the active five-minute window when a rider tries to publish again."""

    rider = user_factory()
    active = ride_factory(rider_id=rider.id, state="Open", bid_until=NOW + 125)
    rider.active_ride = active.id
    draft = ride_factory(rider_id=rider.id)
    with pytest.raises(RuleError, match="125 seconds remain"):
        Rides(db, NOW).publish(rider.id, draft, rider)


def test_offer_creation_revision_and_rejections(db, user_factory, ride_factory):
    """Accept valid available-driver quotes and reject invalid, own, and closed quotes."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, state="Open", bid_until=NOW + 300, choose_until=NOW + 600)
    service = Rides(db, NOW)
    result = service.offer(driver.id, ride, driver, {"price_cents": 2500, "generation": 1})
    revised = service.offer(driver.id, ride, driver, {"price_cents": 2750, "generation": 1})
    assert result["offer_id"] == revised["offer_id"]
    assert revised["revision"] == 2
    assert db.get(Offer, result["offer_id"]).price_cents == 2750
    with pytest.raises(RuleError, match="cannot offer"):
        service.offer(rider.id, ride, rider, {"price_cents": 1000, "generation": 1})
    with pytest.raises(RuleError, match="positive CAD"):
        service.offer(driver.id, ride, driver, {"price_cents": 0, "generation": 1})
    with pytest.raises(RuleError, match="closed"):
        Rides(db, NOW + 301).offer(driver.id, ride, driver, {"price_cents": 2500, "generation": 1})


def test_accept_selects_one_driver_and_closes_competing_offers(
    db, user_factory, ride_factory, offer_factory
):
    """Match exactly one available driver, preserve price, and close all conflicting quotes."""

    rider = user_factory()
    driver = user_factory(202, "Selected Driver")
    other_driver = user_factory(303, "Other Driver")
    other_ride = ride_factory(rider_id=404, state="Open", bid_until=NOW + 300, choose_until=NOW + 600)
    ride = ride_factory(rider_id=rider.id, state="Open", bid_until=NOW + 300, choose_until=NOW + 600)
    rider.active_ride = ride.id
    selected = offer_factory(ride, driver.id, 3200)
    competing = offer_factory(ride, other_driver.id, 3500)
    driver_elsewhere = offer_factory(other_ride, driver.id, 2000)
    Rides(db, NOW).accept(
        rider.id,
        ride,
        {"offer_id": selected.id, "offer_revision": 1, "price_cents": 3200},
        {rider.id: rider, driver.id: driver},
    )
    db.flush()
    assert (ride.state, ride.driver_id, ride.price_cents) == ("Matched", driver.id, 3200)
    assert driver.active_ride == ride.id
    assert selected.status == "accepted"
    assert competing.status == driver_elsewhere.status == "closed"
    assert db.scalar(select(Event).where(Event.action == "matched")) is not None


def test_scheduled_acceptance_creates_due_reminders(db, user_factory, ride_factory, offer_factory):
    """Schedule both supported reminders when a future ride is matched early enough."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=NOW + 300,
        choose_until=NOW + 600,
        scheduled_at=NOW + 7200,
    )
    rider.active_ride = ride.id
    offer = offer_factory(ride, driver.id, 2400)
    Rides(db, NOW).accept(
        rider.id,
        ride,
        {"offer_id": offer.id, "offer_revision": 1, "price_cents": 2400},
        {rider.id: rider, driver.id: driver},
    )
    reminders = db.scalars(select(Job).where(Job.kind == "reminder").order_by(Job.due_at)).all()
    assert [row.due_at for row in reminders] == [NOW + 5400, NOW + 6900]


def test_depart_pickup_double_confirmation_and_completion(
    db, user_factory, ride_factory, location_factory
):
    """Require fresh nearby driver fixes and both pickup confirmations through completion."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Matched", price_cents=3000)
    rider.active_ride = driver.active_ride = ride.id
    users = {rider.id: rider, driver.id: driver}
    Rides(db, NOW).perform(driver.id, "depart", {}, ride, users)
    location_factory(ride, driver.id, sampled_at=NOW + 10)
    Rides(db, NOW + 20).perform(driver.id, "start", {}, ride, users)
    attempt = ride.pickup_attempt
    assert ride.state == "Pickup_Confirmation_Pending"
    assert attempt
    Rides(db, NOW + 30).perform(
        rider.id, "pickup", {"attempt": attempt, "accepted": True}, ride, users
    )
    assert ride.state == "Trip_Started"
    destination = ride.details["destination"]
    location_factory(
        ride,
        driver.id,
        latitude=destination["latitude"],
        longitude=destination["longitude"],
        sampled_at=NOW + 100,
    )
    Rides(db, NOW + 110).perform(driver.id, "complete", {}, ride, users)
    sample = db.get(TrainingSample, ride.id)
    assert ride.state == "Completed"
    assert rider.active_ride is driver.active_ride is None
    assert sample.agreed_price_cents == 3000
    assert sample.pickup_city == "Thunder Bay"


def test_driver_cancel_and_rider_reopen(db, user_factory, ride_factory, offer_factory):
    """Release a cancelling driver and let the rider reopen a new offer generation."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Matched", price_cents=2500)
    rider.active_ride = driver.active_ride = ride.id
    offer = offer_factory(ride, driver.id, status="accepted")
    service = Rides(db, NOW)
    service.cancel(driver.id, ride, {rider.id: rider, driver.id: driver})
    assert ride.state == "Awaiting_Rider"
    assert ride.driver_id is None and driver.active_ride is None
    assert offer.status == "closed"
    service.perform(rider.id, "reopen", {}, ride, {rider.id: rider})
    assert ride.state == "Open" and ride.generation == 2
    assert ride.bid_until == NOW + 300


def test_expiry_selection_final_expiry_and_generation_guard(
    db, user_factory, ride_factory, offer_factory
):
    """Advance bidding to selection then expiry without allowing stale generations to mutate rides."""

    rider = user_factory()
    ride = ride_factory(rider_id=rider.id, state="Open", bid_until=NOW, choose_until=NOW + 300)
    rider.active_ride = ride.id
    offer_factory(ride)
    Rides(db, NOW + 1).expire(ride.id, 99)
    assert ride.state == "Open"
    Rides(db, NOW + 1).expire(ride.id, 1)
    assert ride.state == "Selecting"
    Rides(db, NOW + 301).expire(ride.id, 1)
    assert ride.state == "Expired"
    assert rider.active_ride is None


def test_commands_reject_invalid_ownership_expiry_and_replay_once(db, user_factory, ride_factory):
    """Protect confirmations by action, owner, revision, expiration, and stored idempotent results."""

    rider = user_factory()
    stranger = user_factory(303, "Stranger")
    ride = ride_factory(rider_id=rider.id)
    service = Rides(db, NOW)
    with pytest.raises(RuleError, match="Unsupported"):
        service.propose(rider.id, "delete", {})
    command = service.propose(rider.id, "publish", {"ride_id": ride.id, "revision": ride.revision})
    with pytest.raises(RuleError, match="not found"):
        service.confirm(stranger.id, command.id)
    first = service.confirm(rider.id, command.id)
    second = service.confirm(rider.id, command.id)
    assert first == second
    assert len(db.scalars(select(Event).where(Event.action == "published")).all()) == 1
    expired = Command(
        actor_id=rider.id,
        action="cancel",
        args={"ride_id": ride.id, "revision": ride.revision},
        expires_at=NOW - 1,
    )
    db.add(expired)
    db.flush()
    with pytest.raises(RuleError, match="expired"):
        service.confirm(rider.id, expired.id)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"latitude": 91.0}, "invalid or stale"),
        ({"sampled_at": NOW - 61}, "invalid or stale"),
        ({"sampled_at": NOW + 11}, "invalid or stale"),
        ({"accuracy": -1.0}, "accuracy"),
    ],
)
def test_location_rejects_invalid_samples(db, user_factory, ride_factory, values, message):
    """Reject invalid coordinate, clock, and accuracy data at the domain boundary."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route")
    payload = {
        "latitude": 48.4,
        "longitude": -89.25,
        "accuracy": 20.0,
        "sampled_at": NOW,
        "source": "miniapp",
    }
    payload.update(values)
    with pytest.raises(RuleError, match=message):
        Rides(db, NOW).location(driver.id, ride.id, **payload)


def test_location_order_and_proximity_rules(db, user_factory, ride_factory):
    """Ignore out-of-order samples and require a fresh accurate fix within 500 metres."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route")
    service = Rides(db, NOW)
    service.location(driver.id, ride.id, 48.4, -89.25, 15, NOW, "miniapp")
    service.location(driver.id, ride.id, 48.41, -89.25, 15, NOW - 1, "miniapp")
    db.flush()
    assert len(db.scalars(select(Location).where(Location.ride_id == ride.id)).all()) == 1
    service.near(ride, "pickup")
    latest = service.latest_location(ride, driver.id)
    latest.latitude = 49.0
    with pytest.raises(RuleError, match="500 metres"):
        service.near(ride, "pickup")
    latest.latitude = 48.4
    latest.sampled_at = NOW - 61
    with pytest.raises(RuleError, match="fresh driver"):
        service.near(ride, "pickup")


@pytest.mark.parametrize(
    ("samples", "expected_flag"),
    [
        ([(0, 48.4, -89.25, 150), (30, 48.401, -89.25, 10)], "poor_accuracy"),
        ([(0, 48.4, -89.25, 10), (180, 48.401, -89.25, 10)], "tracking_gap"),
        ([(0, 48.4, -89.25, 10), (1, 49.4, -89.25, 10)], "implausible_jump"),
    ],
)
def test_training_flags_low_quality_tracks(
    db, user_factory, ride_factory, location_factory, samples, expected_flag
):
    """Flag poor accuracy, long gaps, and physically implausible route samples."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(
        rider_id=rider.id,
        driver_id=driver.id,
        state="Trip_Started",
        price_cents=2200,
        started_at=NOW,
    )
    for offset, latitude, longitude, accuracy in samples:
        location_factory(
            ride,
            driver.id,
            sampled_at=NOW + offset,
            latitude=latitude,
            longitude=longitude,
            accuracy=accuracy,
        )
    service = Rides(db, NOW + max(row[0] for row in samples))
    service.training(ride)
    db.flush()
    sample = db.get(TrainingSample, ride.id)
    assert expected_flag in sample.quality_flags
    assert sample.eligible is False


def test_cleanup_applies_each_retention_boundary(
    db, user_factory, ride_factory, location_factory, job_factory
):
    """Delete or de-identify location, personal, command, job, event, and annual training data."""

    rider = user_factory(context={"updated_at": NOW - 31 * 86400, "history": ["private"]})
    ride = ride_factory(rider_id=rider.id, state="Completed", ended_at=NOW - 31 * 86400)
    location_factory(ride, rider.id, sampled_at=NOW - 32 * 86400)
    db.add(
        TrainingSample(
            completion_ref=ride.id,
            distance_metres=1000,
            agreed_price_cents=2000,
            pickup_city="Thunder Bay",
            completed_at=NOW - 366 * 86400,
            distance_source="driver_gps",
            coverage=1,
            quality_flags=[],
            eligible=True,
        )
    )
    db.add(Command(actor_id=rider.id, action="cancel", args={}, expires_at=NOW, created_at=NOW - 31 * 86400))
    job_factory(status="done", created_at=NOW - 31 * 86400)
    db.add(Event(ride_id=ride.id, actor_id=rider.id, action="old", created_at=NOW - 91 * 86400))
    db.flush()
    Rides(db, NOW).cleanup()
    db.flush()
    assert db.get(TrainingSample, ride.id) is None
    assert db.scalars(select(Location)).all() == []
    assert ride.details["pickup"] == {"city": "Ontario"}
    assert rider.context == {}
    assert db.scalars(select(Command)).all() == []
    assert db.scalars(select(Job)).all() == []
    assert db.scalars(select(Event)).all() == []
