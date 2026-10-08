"""Verify private Telegram update handling, callbacks, locations, and rendered controls."""

import time

import pytest
from sqlalchemy import select

from servicehub.api import app as api_module
from servicehub.database.tables import Command, Job, Location, User
from servicehub.integrations.telegram import handlers, messaging
from servicehub.rides.domain import Rides, RuleError


def private_update(update_id=1, user_id=101, text="/start ride", **message_values):
    """Build one private user-authored Telegram message update."""

    message = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Rider", "is_bot": False},
        "text": text,
    }
    message.update(message_values)
    return {"update_id": update_id, "message": message}


def callback_update(data, update_id=1, user_id=101):
    """Build one callback query originating from a private bot message."""

    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "first_name": "Rider", "is_bot": False},
            "data": data,
            "message": {"chat": {"id": user_id, "type": "private"}},
        },
    }


def test_handler_ignores_group_channel_and_bot_updates(db):
    """Ignore public-chat and bot-authored updates before any user record is created."""

    group = private_update()
    group["message"]["chat"]["type"] = "group"
    bot = private_update(update_id=2)
    bot["message"]["from"]["is_bot"] = True
    handlers.handle_update(group)
    handlers.handle_update(bot)
    db.expire_all()
    assert db.scalars(select(User)).all() == []
    assert db.scalars(select(Job)).all() == []


def test_first_private_start_creates_user_and_welcome_job(db):
    """Create a minimal user and enqueue the ride welcome response on first contact."""

    handlers.handle_update(private_update())
    db.expire_all()
    user = db.get(User, 101)
    job = db.scalar(select(Job).where(Job.kind == "send"))
    assert user.name == "Rider" and user.mode == "ride"
    assert "Welcome to ServiceHub" in job.payload["text"]
    assert job.payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "Need a Ride"


def test_smart_support_myride_and_help_entry_paths(db, user_factory, ride_factory, monkeypatch):
    """Show the Smart Support placeholder and render current or empty ride help safely."""

    user = user_factory()
    monkeypatch.setattr(
        handlers,
        "answer",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Smart Support must not invoke the agent yet")),
    )
    handlers.handle_text(user.id, "/start support", 1)
    handlers.handle_text(user.id, "/help", 2)
    ride = ride_factory(rider_id=user.id, state="Open", bid_until=time.time() + 300, choose_until=time.time() + 600)
    user.active_ride = ride.id
    db.commit()
    handlers.handle_text(user.id, "/myride", 3)
    db.expire_all()
    jobs = db.scalars(select(Job).where(Job.kind == "send").order_by(Job.created_at)).all()
    assert any("Smart Support is coming soon" in job.payload["text"] for job in jobs)
    assert any("Welcome to ServiceHub" in job.payload["text"] for job in jobs)
    assert any(ride.id[:8] in job.payload["text"] for job in jobs)


def test_offer_entry_and_price_create_confirmation(
    db, user_factory, ride_factory, monkeypatch
):
    """Verify offer deep links, membership, price parsing, and confirmation creation."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=time.time() + 300,
        choose_until=time.time() + 600,
    )
    db.commit()
    checked = []
    monkeypatch.setattr(handlers, "require_member", lambda actor: checked.append(actor))
    handlers.handle_text(driver.id, f"/start offer_{ride.id}", 1)
    handlers.handle_text(driver.id, "CAD 25.50", 2)
    db.expire_all()
    command = db.scalar(select(Command).where(Command.actor_id == driver.id))
    assert checked == [driver.id]
    assert command.action == "offer"
    assert command.args["price_cents"] == 2550
    assert command.result is None
    assert any(
        "Submit Offer" in str(job.payload.get("reply_markup", {}))
        for job in db.scalars(select(Job).where(Job.kind == "send"))
    )


def test_callback_acknowledges_and_confirms_owned_command(db, user_factory, monkeypatch):
    """Acknowledge callbacks and delegate only owned command confirmations."""

    user_factory()
    command = Command(actor_id=101, action="cancel", args={}, expires_at=time.time() + 300)
    db.add(command)
    db.commit()
    telegram_calls = []
    confirmations = []
    monkeypatch.setattr(handlers, "telegram", lambda method, payload: telegram_calls.append((method, payload)) or {})
    monkeypatch.setattr(api_module, "confirm_command", lambda actor, command_id: confirmations.append((actor, command_id)))
    handlers.handle_update(callback_update(f"confirm:{command.id}"))
    assert telegram_calls[0][0] == "answerCallbackQuery"
    assert confirmations == [(101, command.id)]
    db.expire_all()
    assert any(job.kind == "send" for job in db.scalars(select(Job)))


def test_review_callback_enforces_owner_expiry_and_unchanged_arguments(
    db, user_factory, ride_factory
):
    """Reject review callbacks that are unowned, expired, or stale against current ride state."""

    rider = user_factory()
    other = user_factory(202, "Other")
    ride = ride_factory(rider_id=rider.id)
    command = Command(
        actor_id=rider.id,
        action="publish",
        args={"ride_id": ride.id, "revision": ride.revision, "generation": ride.generation},
        expires_at=time.time() + 300,
    )
    expired = Command(
        actor_id=rider.id,
        action="publish",
        args=command.args,
        expires_at=time.time() - 1,
    )
    db.add_all([command, expired])
    db.commit()
    with pytest.raises(RuleError, match="expired"):
        handlers.handle_callback(other.id, f"review:{command.id}")
    with pytest.raises(RuleError, match="expired"):
        handlers.handle_callback(rider.id, f"review:{expired.id}")
    ride.revision += 1
    db.commit()
    with pytest.raises(RuleError, match="changed"):
        handlers.handle_callback(rider.id, f"review:{command.id}")


def test_location_callbacks_share_instructions_and_counterpart_fix(
    db, user_factory, ride_factory, location_factory
):
    """Render sharing guidance and enqueue a Telegram location only for active participants."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route")
    location_factory(ride, driver.id, sampled_at=time.time())
    db.commit()
    handlers.handle_callback(rider.id, "share")
    handlers.handle_callback(rider.id, f"location:{ride.id}")
    db.expire_all()
    kinds = [job.kind for job in db.scalars(select(Job)).all()]
    assert "send_location" in kinds
    assert kinds.count("send") >= 2
    with pytest.raises(RuleError, match="unavailable"):
        handlers.handle_callback(303, f"location:{ride.id}")


def test_live_location_is_stored_and_ordinary_location_becomes_private_error(
    db, user_factory, ride_factory
):
    """Accept native live updates and turn ordinary-location misuse into a recoverable response."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route")
    driver.active_ride = ride.id
    db.commit()
    live = private_update(
        update_id=10,
        user_id=driver.id,
        text="",
        location={
            "latitude": 48.4,
            "longitude": -89.25,
            "horizontal_accuracy": 12,
            "live_period": 900,
        },
    )
    handlers.handle_update(live)
    ordinary = private_update(
        update_id=11,
        user_id=driver.id,
        text="",
        location={"latitude": 48.4, "longitude": -89.25},
    )
    handlers.handle_update(ordinary)
    db.expire_all()
    assert len(db.scalars(select(Location).where(Location.user_id == driver.id)).all()) == 1
    error = db.scalar(select(Job).where(Job.dedupe == "error:11"))
    assert "share a live location" in error.payload["text"]


def test_channel_markdown_and_controls_preserve_privacy(db, user_factory, ride_factory):
    """Escape MarkdownV2, keep the pinned layout stable, and omit private address details."""

    rider = user_factory()
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=time.time() + 300,
        choose_until=time.time() + 600,
    )
    service = Rides(db)
    view = {
        "pickup": "Sherrington Drive, Thunder Bay",
        "destination": "Arthur Street (West), Thunder Bay",
        "scheduled_label": "Immediate",
    }
    text = messaging.channel_ride_text(ride, view)
    assert f"Ride ID: *{ride.id[:8].upper()}*" in text
    assert "Source: *Sherrington Drive, Thunder Bay*" in text
    assert r"Arthur Street \(West\)" in text
    assert "Unit" not in text and "4B" not in text
    welcome = messaging.welcome_buttons()["inline_keyboard"]
    assert [row[0]["text"] for row in welcome] == ["Need a Ride", "Smart Support"]
    assert welcome[1][0]["url"].endswith("?start=support")
    buttons = messaging.ride_buttons(service, rider.id, ride)["inline_keyboard"]
    assert buttons[0][0]["text"] == "My Ride"
    assert any(row[0]["text"] == "Cancel Ride" for row in buttons)
