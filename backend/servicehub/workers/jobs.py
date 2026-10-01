"""Process durable inbox/outbox jobs with leases, retries, scheduled recovery, and GCP dispatch."""

import logging
import time

from sqlalchemy import select

from servicehub.core.config import settings
from servicehub.database.session import Session
from servicehub.database.tables import Job, Offer, Ride, User, uid
from servicehub.integrations.providers import ProviderError, telegram
from servicehub.integrations.telegram.messaging import event_messages, render
from servicehub.rides.domain import Rides, enqueue
from servicehub.rides.views import ride_view

log = logging.getLogger("servicehub.jobs")

def run_job(job_id: str) -> None:
    """Claim one job, perform its effect, and acknowledge only the matching lease."""

    now = time.time()
    with Session.begin() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status in {"done", "dead"} or job.due_at > now or job.lease_until > now:
            return
        job.status, job.lease_until, job.lease_token = "running", now + 180, uid()
        job.attempts += 1
        kind, payload, token = job.kind, dict(job.payload), job.lease_token
    error = None
    permanent, delay = False, 30
    try:
        execute(kind, payload, job_id)
    except ProviderError as exc:
        error, permanent, delay = str(exc), exc.permanent, exc.retry_after
    except Exception as exc:
        error = type(exc).__name__
    with Session.begin() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.lease_token != token:
            return
        if error:
            job.status = "dead" if permanent or job.attempts >= 8 else "pending"
            job.error = error[:128]
            job.due_at = time.time() + max(delay, min(900, 2 ** job.attempts))
            log.warning("job_failed id=%s kind=%s status=%s error=%s", job.id, kind, job.status, error)
        else:
            job.status, job.error = "done", None
        job.lease_until, job.lease_token = 0, None

def execute(kind: str, payload: dict, job_id: str) -> None:
    """Dispatch typed jobs while keeping external delivery out of domain transactions."""

    if kind == "telegram_update":
        from servicehub.integrations.telegram.handlers import handle_update
        handle_update(payload)
        return
    if kind in {"send", "send_location"}:
        ride_id = payload.pop("_ride_id", None) or payload.pop("ride_id", None)
        revision = payload.pop("_revision", None)
        if ride_id:
            with Session() as db:
                ride = db.get(Ride, ride_id)
                if not ride or (revision and revision != ride.revision):
                    return
                if kind == "send_location" and ride.state not in {"Driver_En_Route", "Pickup_Confirmation_Pending", "Trip_Started"}:
                    return
        telegram("sendLocation" if kind == "send_location" else "sendMessage", payload)
        return
    if kind == "channel":
        update_channel(payload["ride_id"])
        return
    with Session.begin() as db:
        service = Rides(db)
        if kind == "cleanup":
            service.cleanup()
            return
        if kind == "direct":
            enqueue(db, "send", {"chat_id": payload["chat_id"], "text": render(db, payload["key"], payload["values"])}, time.time(), f"delivery:{job_id}")
            return
        if kind == "offer_event":
            offer = db.get(Offer, payload["offer_id"])
            if not offer or offer.revision != payload["revision"]:
                return
            ride = db.get(Ride, offer.ride_id)
            driver = db.get(User, offer.driver_id)
            if offer.status == "pending" and ride.state in {"Open", "Selecting"}:
                from servicehub.integrations.telegram.messaging import form_button
                enqueue(db, "send", {"chat_id": ride.rider_id,
                    "text": render(db, "offer", {"driver": driver.name, "price": f"{offer.price_cents / 100:.2f}", "reference": ride.id[:8]}),
                    "reply_markup": {"inline_keyboard": [[form_button("Review Offers", ride.id)]]}}, time.time(), f"delivery:{job_id}")
            return
        ride = db.get(Ride, payload.get("ride_id", ""))
        if not ride:
            return
        if kind == "expiry":
            service.expire(ride.id, payload["generation"])
        elif kind == "pickup_expiry":
            service.users(ride.rider_id, ride.driver_id)
            ride = service.ride(ride.id)
            if ride.state == "Pickup_Confirmation_Pending" and ride.pickup_attempt == payload["attempt"] and time.time() >= (ride.pickup_until or 0):
                ride.state, ride.pickup_attempt = "Driver_En_Route", None
                service.notify(ride, "pickup_expired", ride.rider_id)
        elif kind in {"ride_event", "reminder"}:
            if kind == "reminder":
                if ride.generation != payload["generation"] or ride.state not in {"Matched", "Driver_En_Route"}:
                    return
                action = "reminder"
            else:
                if ride.revision != payload["revision"]:
                    return
                action = payload["action"]
            for index, message in enumerate(event_messages(service, ride, action)):
                enqueue(db, "send", {**message, "_ride_id": ride.id, "_revision": ride.revision}, time.time(), f"delivery:{job_id}:{index}")
            if kind == "ride_event":
                enqueue(db, "channel", {"ride_id": ride.id}, time.time(), f"channel:{job_id}")

def update_channel(ride_id: str) -> None:
    """Serialize channel edits on the ride and always render the latest public state."""

    with Session.begin() as db:
        ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
        if not ride or ride.state == "Draft":
            return
        view = ride_view(Rides(db), 0, ride)
        if ride.state == "Open":
            text = render(db, "request", {"pickup": view["pickup"], "destination": view["destination"], "schedule": view["scheduled_label"], "reference": ride.id[:8]})
            buttons = [[{"text": "Make an Offer", "url": f"https://t.me/{settings().telegram_bot_username}?start=offer_{ride.id}"}]]
        else:
            text = render(db, "status", {"reference": ride.id[:8], "state": ride.state, "details": f"{view['pickup']} → {view['destination']}"})
            buttons = []
        body = {"chat_id": settings().telegram_channel_id, "text": text, "reply_markup": {"inline_keyboard": buttons}}
        if ride.channel_message:
            telegram("editMessageText", {**body, "message_id": ride.channel_message})
        else:
            result = telegram("sendMessage", body)
            ride.channel_message = result["message_id"]

def dispatch() -> None:
    """Enqueue due database jobs into Cloud Tasks; leases tolerate duplicate task delivery."""

    from google.cloud import tasks_v2

    config = settings()
    queue = tasks_v2.CloudTasksClient()
    parent = queue.queue_path(config.gcp_project, config.gcp_location, config.task_queue)
    with Session() as db:
        jobs = db.scalars(select(Job).where(Job.status.in_(["pending", "running"]), Job.due_at <= time.time(), Job.lease_until <= time.time()).limit(100)).all()
        for job in jobs:
            task = {"http_request": {"http_method": tasks_v2.HttpMethod.POST,
                    "url": f"{config.worker_url}/internal/jobs/{job.id}",
                    "oidc_token": {"service_account_email": config.worker_service_account, "audience": config.internal_audience},
                    "headers": {"Content-Type": "application/json"}, "body": b"{}"}}
            queue.create_task(parent=parent, task=task)

def run_local() -> None:
    """Poll the same durable queue locally without requiring Redis or GCP."""

    while True:
        with Session.begin() as db:
            enqueue(db, "cleanup", {}, time.time(), f"cleanup:{int(time.time() // 86400)}")
            ids = list(db.scalars(select(Job.id).where(Job.status.in_(["pending", "running"]), Job.due_at <= time.time(), Job.lease_until <= time.time()).order_by(Job.due_at).limit(30)))
        for job_id in ids:
            run_job(job_id)
        time.sleep(1)
