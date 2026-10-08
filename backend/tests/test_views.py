"""Verify role-filtered ride projections and immutable confirmation summaries."""

import pytest
from conftest import NOW

from servicehub.rides.domain import Rides, RuleError
from servicehub.rides.views import command_summary, ride_view, visible_ride


def test_role_views_reveal_exact_addresses_only_to_participants(
    db, user_factory, ride_factory, offer_factory
):
    """Hide exact address, unit, instructions, and unrelated offers from observers."""

    rider = user_factory()
    driver = user_factory(202, "Selected Driver")
    observer = user_factory(303, "Observer")
    ride = ride_factory(
        rider_id=rider.id,
        driver_id=driver.id,
        state="Open",
        bid_until=NOW + 300,
        choose_until=NOW + 600,
    )
    offer_factory(ride, driver.id)
    service = Rides(db, NOW)
    rider_view = ride_view(service, rider.id, ride)
    driver_view = ride_view(service, driver.id, ride)
    observer_view = ride_view(service, observer.id, ride)
    assert rider_view["exact_pickup"] == driver_view["exact_pickup"]
    assert rider_view["unit"] == "4B"
    assert "exact_pickup" not in observer_view
    assert "Unit" not in observer_view["pickup"]
    assert observer_view["offers"] == []


def test_closed_ride_visibility_requires_participation(db, user_factory, ride_factory):
    """Permit public bidding views only while a ride remains open."""

    rider = user_factory()
    observer = user_factory(303, "Observer")
    ride = ride_factory(rider_id=rider.id, state="Completed", ended_at=NOW)
    service = Rides(db, NOW)
    assert visible_ride(service, rider.id, ride.id)["role"] == "rider"
    with pytest.raises(RuleError, match="unavailable"):
        visible_ride(service, observer.id, ride.id)


def test_command_summary_binds_revision_generation_and_offer_price(
    db, user_factory, ride_factory, offer_factory
):
    """Bind proposal arguments to current ride and offer versions before confirmation."""

    rider = user_factory()
    ride = ride_factory(rider_id=rider.id, state="Open", bid_until=NOW + 300, choose_until=NOW + 600)
    offer = offer_factory(ride, price_cents=2875)
    args = {"ride_id": ride.id, "offer_id": offer.id, "offer_revision": offer.revision}
    summary = command_summary(Rides(db, NOW), rider.id, "accept", args)
    assert "CAD 28.75" in summary
    assert args == {
        "ride_id": ride.id,
        "offer_id": offer.id,
        "offer_revision": 1,
        "revision": ride.revision,
        "generation": ride.generation,
        "price_cents": 2875,
    }
    offer.revision += 1
    with pytest.raises(RuleError, match="Offer changed"):
        command_summary(Rides(db, NOW), rider.id, "accept", dict(args, offer_revision=1))


@pytest.mark.parametrize(
    ("action", "actor", "message"),
    [("publish", 303, "rider-only"), ("depart", 101, "driver-only"), ("cancel", 303, "not your ride")],
)
def test_command_summary_enforces_action_ownership(
    db, user_factory, ride_factory, action, actor, message
):
    """Reject confirmation summaries requested by users without the required ride role."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    user_factory(303, "Observer")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Matched")
    with pytest.raises(RuleError, match=message):
        command_summary(Rides(db, NOW), actor, action, {"ride_id": ride.id})
