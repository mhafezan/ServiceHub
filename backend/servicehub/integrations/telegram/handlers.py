"""Translate Telegram updates into authenticated proposals and private service conversations."""

import time
from decimal import Decimal, InvalidOperation

from servicehub.agents.runtime import answer
from servicehub.core.config import settings
from servicehub.database.session import Session
from servicehub.database.tables import Command, Ride, User
from servicehub.integrations.providers import ProviderError, require_member, telegram
from servicehub.integrations.telegram.messaging import form_button, render, ride_buttons
from servicehub.rides.domain import TRACKABLE, Rides, RuleError, enqueue
from servicehub.rides.views import command_summary, visible_ride


def send_later(db, chat_id: int, text: str, buttons: dict | None = None, dedupe: str | None = None) -> None:
    """Persist a private response rather than sending inside a business transaction."""

    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = buttons
    enqueue(db, "send", payload, time.time(), dedupe)


def handle_update(update: dict) -> None:
    """Process private messages, signed callbacks, and native live-location edits."""

    callback = update.get("callback_query")
    message = update.get("message") or update.get("edited_message") or (callback or {}).get("message", {})
    if message.get("chat", {}).get("type") != "private":
        return
    identity = (callback or message).get("from", {})
    actor = identity.get("id")
    if not actor or identity.get("is_bot"):
        return
    if callback:
        try:
            telegram("answerCallbackQuery", {"callback_query_id": callback["id"]})
        except ProviderError:
            pass
    with Session.begin() as db:
        user = db.get(User, actor)
        if not user:
            user = User(id=actor, name=identity.get("first_name", "Member")[:128])
            db.add(user)
            db.flush()
    try:
        if callback:
            handle_callback(actor, callback.get("data", ""))
        elif "location" in message:
            with Session.begin() as db:
                user = db.get(User, actor)
                if not user.active_ride:
                    return
                point = message["location"]
                if not point.get("live_period"):
                    raise RuleError("Please share a live location or use Share My Location in the Mini App")
                service = Rides(db)
                service.location(actor, user.active_ride, point["latitude"], point["longitude"], point.get("horizontal_accuracy"), message.get("edit_date", message["date"]), "telegram")
                if "edited_message" not in update:
                    send_later(db, actor, render(db, "success"), {"inline_keyboard": [[form_button()]]})
        else:
            handle_text(actor, message.get("text", "")[:4000], update["update_id"])
    except (RuleError, InvalidOperation, ValueError) as exc:
        with Session.begin() as db:
            send_later(db, actor, render(db, "error", {"reason": str(exc)}), {"inline_keyboard": [[form_button()]]}, f"error:{update['update_id']}")

def handle_callback(actor: int, data: str) -> None:
    """Require owned confirmation records and authorize every location-view callback."""

    from servicehub.api.app import confirm_command

    if data.startswith("confirm:"):
        confirm_command(actor, data.split(":", 1)[1])
        with Session.begin() as db:
            send_later(db, actor, render(db, "success"), {"inline_keyboard": [[form_button()]]})
        return
    with Session.begin() as db:
        service = Rides(db)
        if data.startswith("review:"):
            command = db.get(Command, data.split(":", 1)[1])
            if not command or command.actor_id != actor or command.expires_at < time.time():
                raise RuleError("This action expired; refresh My Ride")
            args = dict(command.args)
            summary = command_summary(service, actor, command.action, args)
            if args != command.args:
                raise RuleError("This action changed; refresh My Ride")
            send_later(db, actor, render(db, "confirmation", {"summary": summary}), {"inline_keyboard": [[{"text": "Confirm", "callback_data": f"confirm:{command.id}"}]]})
        elif data == "share":
            send_later(db, actor, render(db, "share"), {"inline_keyboard": [[form_button("Share My Location")]]})
        elif data.startswith("location:"):
            ride = db.get(Ride, data.split(":", 1)[1])
            if not ride or actor not in {ride.rider_id, ride.driver_id} or ride.state not in TRACKABLE:
                raise RuleError("Location access is unavailable")
            fix = service.latest_location(ride, ride.driver_id if actor == ride.rider_id else ride.rider_id)
            if fix:
                age = round(time.time() - fix.sampled_at)
                send_later(db, actor, render(db, "location", {"seconds": age, "accuracy": fix.accuracy if fix.accuracy is not None else "unknown", "freshness": "Stale location" if age > 60 else "Recent location"}), {"inline_keyboard": [[form_button("Refresh Location", ride.id)]]})
                enqueue(db, "send_location", {"chat_id": actor, "latitude": fix.latitude, "longitude": fix.longitude, "ride_id": ride.id}, time.time())
            else:
                send_later(db, actor, render(db, "unavailable"))

def handle_text(actor: int, text: str, update_id: int) -> None:
    """Handle entry links, price proposals, and bounded supervisor conversations."""
    
    explicit = None
    if text.startswith("/start offer_"):
        require_member(actor)
    with Session.begin() as db:
        service = Rides(db)
        user = db.get(User, actor)
        if text.startswith("/start"):
            parameter = text.partition(" ")[2]
            if parameter.startswith("offer_"):
                ride_id = parameter.removeprefix("offer_")
                visible_ride(service, actor, ride_id)
                user.context = {"offer_ride": ride_id, "updated_at": time.time()}
                user.mode = "ride"
                send_later(db, actor, render(db, "offer_prompt"), {"inline_keyboard": [[form_button("Review Ride", ride_id)]]})
                return
            user.context = {}
            user.mode = "lili" if parameter == "lili" else "ride"
            if parameter != "lili":
                send_later(db, actor, render(db, "welcome"), {"inline_keyboard": [[form_button("Need a Ride")]]})
                return
            text, explicit = "How can I use ServiceHub?", "lili"
        if text in {"/myride", "/help"}:
            ride = db.get(Ride, user.active_ride) if user.active_ride else None
            if ride:
                send_later(db, actor, render(db, "status", {"reference": ride.id[:8], "state": ride.state, "details": ""}), ride_buttons(service, actor, ride))
            else:
                send_later(db, actor, render(db, "welcome"), {"inline_keyboard": [[form_button("Need a Ride")], [{"text": "Ride Quick Guide", "url": settings().public_url + "/guides/ride"}]]})
            return
        if user.context.get("offer_ride"):
            value = Decimal(text.strip().removeprefix("$").replace("CAD", "").strip())
            if not value.is_finite() or value != value.quantize(Decimal("0.01")):
                raise RuleError("Use at most two decimal places")
            args = {"ride_id": user.context["offer_ride"], "price_cents": int(value * 100)}
            summary = command_summary(service, actor, "offer", args)
            command = service.propose(actor, "offer", args)
            user.context = {}
            send_later(db, actor, render(db, "confirmation", {"summary": summary}), {"inline_keyboard": [[{"text": "Submit Offer", "callback_data": f"confirm:{command.id}"}]]})
            return
        try:
            response = answer(service, actor, text, explicit or (user.mode if user.mode in {"ride", "lili"} else None))
        except Exception:
            response = {"text": "Conversational assistance is temporarily unavailable. Please use My Ride or the form.", "open_form": True}
        rows = [[form_button()]] if response.get("open_form") else []
        for command in response.get("commands", []):
            rows.append([{"text": "Review Action", "callback_data": f"review:{command['id']}"}])
        send_later(db, actor, response["text"], {"inline_keyboard": rows} if rows else None, f"answer:{update_id}")
