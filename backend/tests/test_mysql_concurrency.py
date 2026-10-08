"""Exercise transaction races that require a dedicated disposable MySQL schema."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import NOW
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from servicehub.database.tables import Command, Offer, Ride, User
from servicehub.rides.domain import Rides, RuleError

pytestmark = pytest.mark.mysql


def test_mysql_job_timestamp_round_trip(mysql_session):
    """Preserve subsecond job timestamps and immediately select due work in MySQL."""

    from servicehub.database.tables import Job

    due = NOW + 0.125
    job = Job(dedupe="mysql-time", kind="cleanup", payload={}, due_at=due, status="pending", lease_until=0)
    mysql_session.add(job)
    mysql_session.commit()
    mysql_session.expire_all()
    assert mysql_session.get(Job, job.id).due_at == pytest.approx(due, abs=0.001)
    assert mysql_session.scalar(select(Job).where(Job.due_at <= due)) is not None


def test_mysql_concurrent_confirmation_executes_once(mysql_session):
    """Serialize duplicate confirmations so a command's mutation is recorded once."""

    database_url = str(mysql_session.bind.url)
    rider = User(id=101, name="Rider", active_ride=None, mode="ride", context={})
    ride = Ride(
        rider_id=101,
        state="Draft",
        revision=1,
        generation=1,
        details={
            "pickup": {"city": "Thunder Bay", "street": "A"},
            "destination": {"city": "Thunder Bay", "street": "B"},
            "training_city": "Thunder Bay",
        },
        timezone="America/Toronto",
    )
    mysql_session.add_all([rider, ride])
    mysql_session.flush()
    command = Command(
        actor_id=101,
        action="publish",
        args={"ride_id": ride.id, "revision": 1},
        expires_at=NOW + 300,
    )
    mysql_session.add(command)
    mysql_session.commit()
    factory = sessionmaker(create_engine(database_url, isolation_level="READ COMMITTED"), expire_on_commit=False)

    def confirm() -> tuple[str, str]:
        """Confirm through an independent database transaction."""

        with factory.begin() as session:
            try:
                result = Rides(session, NOW).confirm(101, command.id)
                return "ok", result["state"]
            except RuleError as exc:
                return "rule", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: confirm(), range(2)))
    assert all(row == ("ok", "Open") for row in results)
    mysql_session.expire_all()
    stored = mysql_session.get(Command, command.id)
    assert stored.result["state"] == "Open"


def test_mysql_competing_offer_acceptance_keeps_one_assignment(mysql_session):
    """Allow one competing acceptance while preserving consistent rider and driver slots."""

    rider = User(id=101, name="Rider", active_ride=None, mode="ride", context={})
    drivers = [
        User(id=202, name="Driver A", active_ride=None, mode="ride", context={}),
        User(id=303, name="Driver B", active_ride=None, mode="ride", context={}),
    ]
    ride = Ride(
        rider_id=101,
        state="Open",
        revision=1,
        generation=1,
        details={
            "pickup": {"city": "Thunder Bay", "street": "A"},
            "destination": {"city": "Thunder Bay", "street": "B"},
            "training_city": "Thunder Bay",
        },
        timezone="America/Toronto",
        bid_until=NOW + 300,
        choose_until=NOW + 600,
    )
    mysql_session.add_all([rider, *drivers, ride])
    mysql_session.flush()
    rider.active_ride = ride.id
    offers = [
        Offer(ride_id=ride.id, driver_id=driver.id, generation=1, price_cents=2000 + index * 500)
        for index, driver in enumerate(drivers)
    ]
    mysql_session.add_all(offers)
    mysql_session.commit()
    factory = sessionmaker(mysql_session.bind, expire_on_commit=False)

    def accept(offer_id: str) -> str:
        """Attempt one selection from an isolated transaction."""

        with factory.begin() as session:
            offer = session.get(Offer, offer_id)
            locked_ride = session.get(Ride, ride.id)
            service = Rides(session, NOW)
            users = service.users(101, offer.driver_id)
            try:
                service.accept(
                    101,
                    locked_ride,
                    {"offer_id": offer.id, "offer_revision": 1, "price_cents": offer.price_cents},
                    users,
                )
                return "accepted"
            except RuleError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, [offer.id for offer in offers]))
    assert sorted(results) == ["accepted", "rejected"]
    mysql_session.expire_all()
    stored_ride = mysql_session.get(Ride, ride.id)
    committed_drivers = mysql_session.scalars(select(User).where(User.active_ride == ride.id)).all()
    assert stored_ride.state == "Matched"
    assert {user.id for user in committed_drivers} == {101, stored_ride.driver_id}
