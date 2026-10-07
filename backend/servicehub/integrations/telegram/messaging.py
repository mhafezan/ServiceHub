"""Render validated cached prose and build Telegram controls from authoritative state."""

import re
from string import Formatter

from servicehub.agents.runtime import client
from servicehub.core.config import settings
from servicehub.database.tables import Ride, Template, User
from servicehub.rides.domain import TRACKABLE, Rides

TEMPLATES = {
    "welcome": "Welcome to ServiceHub. Need a ride in Ontario, or have a question?",
    "request": "Ride requested: {pickup} → {destination}. Pickup: {schedule}. Bidding closes in five minutes. Reference: {reference}.",
    "status": "Ride {reference}: {state}. {details}",
    "offer": "{driver} offered CAD {price} for ride {reference}. Review the current offer before accepting.",
    "matched_driver": "Your offer was accepted by {rider} for CAD {price}. Pickup: {pickup}. Destination: {destination}. {instructions}",
    "matched_rider": "Your driver is selected for CAD {price}. Awaiting departure.",
    "departed": "Your driver is coming to your pickup address. You can view their shared location below.",
    "pickup": "The driver says pickup occurred. Please confirm only if you are with the driver.",
    "started": "Both participants confirmed pickup. Your trip has started.",
    "completed": "Your trip is complete. Thank you for using ServiceHub.",
    "cancelled": "This ride assignment was cancelled. Open My Ride to see the current status.",
    "reopen": "Your driver cancelled. You can reopen your request for fresh offers or cancel it.",
    "selection": "Bidding has closed. You have five more minutes to choose an existing offer.",
    "expired": "Your request expired. You may submit another request now.",
    "reminder": "Your scheduled pickup is approaching. Open My Ride for details.",
    "offer_prompt": "Enter your offer in CAD, for example 25.50. You will review and confirm it before submission.",
    "confirmation": "Please review and confirm: {summary}",
    "success": "Your action was recorded. Open My Ride for the current status.",
    "error": "{reason}",
    "share": "In this private chat, use Telegram's attachment menu → Location → Share live location. You can also share while the Mini App is open.",
    "location": "Latest shared location. Updated {seconds} seconds ago; accuracy {accuracy} metres. {freshness}",
    "unavailable": "No current shared location is available. Ask the other participant to share their location.",
}

MARKDOWN_V2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

def telegram_markdown(value: object) -> str:
    """Escape dynamic text before placing it inside Telegram MarkdownV2 markup."""

    return MARKDOWN_V2_SPECIAL.sub(r"\\\1", str(value))

def channel_ride_text(ride: Ride, view: dict) -> str:
    """Format public ride facts as one bold-valued field per channel-message line."""

    fields = [
        ("Ride ID", ride.id[:8].upper()),
        ("Status", ride.state.replace("_", " ")),
        ("Source", view["pickup"]),
        ("Destination", view["destination"]),
    ]
    if ride.state == "Open":
        fields.extend(
            [
                ("Pickup", view["scheduled_label"]),
                ("Offer Window", "5 minutes"),
            ]
        )
    return "\n".join(f"{label}: *{telegram_markdown(value)}*" for label, value in fields)

def placeholders(text: str) -> set[str]:
    """Extract simple placeholders and reject format expressions or attribute access."""

    fields = set()
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None:
            if not re.fullmatch(r"[a-z_]+", field) or spec or conversion:
                raise ValueError("Unsafe template placeholder")
            fields.add(field)
    return fields

def render(db, key: str, values: dict | None = None) -> str:
    """Use generated cached wording, with explicit local bootstrap fixtures only."""

    template = db.get(Template, key)
    if template:
        text = template.text
    elif settings().environment == "local":
        text = TEMPLATES[key]
    else:
        raise RuntimeError("Message templates must be generated before deployment")
    return text.format(**(values or {}))[:4000]

def generate_templates(db) -> None:
    """Generate and validate every message template before enabling live workflows."""

    import json

    for key, baseline in TEMPLATES.items():
        response = client().responses.create(model=settings().openai_model, store=False,
            instructions="Rewrite this Telegram message concisely, preserving its meaning, every placeholder exactly, and all timing facts. No Markdown, no extra promises. Return JSON with only text.",
            input=baseline, text={"format": {"type": "json_schema", "name": "message", "strict": True,
            "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}})
        text = json.loads(response.output_text)["text"]
        if placeholders(text) != placeholders(baseline) or len(text) > 1000:
            raise ValueError(f"Generated template failed validation: {key}")
        row = db.get(Template, key)
        if row:
            row.text = text
        else:
            db.add(Template(key=key, text=text))

def form_button(label: str = "My Ride", ride_id: str = "") -> dict:
    """Open the authenticated Mini App without embedding private data in URLs."""

    return {"text": label, "web_app": {"url": f"{settings().public_url}/?ride={ride_id}"}}

def welcome_buttons() -> dict:
    """Keep the requested two-row pinned entry layout stable."""

    root = f"https://t.me/{settings().telegram_bot_username}?start="
    return {"inline_keyboard": [[{"text": "Need a Ride", "url": root + "ride"}], [{"text": "Ask Lili", "url": root + "lili"}]]}

def ride_buttons(service: Rides, actor: int, ride: Ride) -> dict:
    """Create owned, version-bound confirmation proposals and tracking controls."""

    from servicehub.rides.views import command_summary

    buttons = [[form_button(ride_id=ride.id)]]
    actions: list[tuple[str, str, dict]] = []
    if actor == ride.driver_id:
        if ride.state == "Matched":
            actions.append(("On My Way", "depart", {}))
        if ride.state in {"Driver_En_Route", "Trip_Started"}:
            endpoint = "pickup" if ride.state == "Driver_En_Route" else "destination"
            try:
                service.near(ride, endpoint)
                if not ride.scheduled_at or service.now >= ride.scheduled_at - 900:
                    actions.append(("Start Trip" if endpoint == "pickup" else "Complete Trip", "start" if endpoint == "pickup" else "complete", {}))
            except ValueError:
                pass
    if actor == ride.rider_id and ride.state == "Pickup_Confirmation_Pending":
        actions += [("Confirm Pickup", "pickup", {"accepted": True}), ("Not Picked Up", "pickup", {"accepted": False})]
    if actor == ride.rider_id and ride.state == "Awaiting_Rider":
        actions.append(("Reopen Request", "reopen", {}))
    if ride.state not in {"Completed", "Cancelled", "Expired", "Trip_Started", "Administrative_Closure"}:
        actions.append(("Cancel Ride", "cancel", {}))
    for label, action, extra in actions:
        args = {"ride_id": ride.id, **extra}
        command_summary(service, actor, action, args)
        command = service.propose(actor, action, args)
        buttons.append([{"text": label, "callback_data": f"review:{command.id}"}])
    if ride.state in TRACKABLE:
        buttons += [[{"text": "Share My Location", "callback_data": "share"}],
                    [{"text": "View Driver Location" if actor == ride.rider_id else "View Rider Location", "callback_data": f"location:{ride.id}"}]]
    return {"inline_keyboard": buttons}

def event_messages(service: Rides, ride: Ride, action: str) -> list[dict]:
    """Project current ride events into private messages after transaction commit."""
    
    db = service.db
    rider = db.get(User, ride.rider_id)
    recipients: list[tuple[int, str, dict]] = []
    if action == "matched" and ride.driver_id:
        recipients += [(ride.driver_id, "matched_driver", {"rider": rider.name, "price": f"{ride.price_cents / 100:.2f}",
            "pickup": ride.details["pickup"]["exact"] + " " + ride.details.get("unit", ""),
            "destination": ride.details["destination"]["exact"], "instructions": ride.details.get("instructions", "")}),
            (ride.rider_id, "matched_rider", {"price": f"{ride.price_cents / 100:.2f}"})]
    else:
        key = {"departed": "departed", "pickup_requested": "pickup", "pickup_confirmed": "started", "completed": "completed", "expired": "expired", "selection": "selection", "reminder": "reminder"}.get(action, "status")
        if ride.state == "Awaiting_Rider":
            key = "reopen"
        values = {"reference": ride.id[:8], "state": ride.state, "details": ""} if key == "status" else {}
        recipients.append((ride.rider_id, key, values))
        if ride.driver_id and action not in {"pickup_requested", "departed"}:
            recipients.append((ride.driver_id, key, values))
    return [{"chat_id": actor, "text": render(db, key, values), "reply_markup": ride_buttons(service, actor, ride)} for actor, key, values in recipients]
