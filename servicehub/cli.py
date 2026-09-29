"""Provide setup, recovery, migration-independent workers, and safe operational commands."""

import argparse
import json
import time

from sqlalchemy import select

from servicehub.config import settings
from servicehub.db import Session
from servicehub.domain import Rides
from servicehub.messaging import generate_templates, render, welcome_buttons
from servicehub.models import Event, Job, Ride, TrainingSample
from servicehub.providers import telegram


def main() -> None:
    """Execute explicit operator commands without printing credentials or private samples."""
    parser = argparse.ArgumentParser(prog="servicehub")
    parser.add_argument("command", choices=["worker", "generate-templates", "setup-telegram", "dead-jobs", "retry", "close-ride", "export-training"])
    parser.add_argument("--id")
    parser.add_argument("--actor", type=int)
    parser.add_argument("--reason", default="")
    parser.add_argument("--output", default="private-data/training.jsonl")
    args = parser.parse_args()
    if args.command == "worker":
        from servicehub.worker import run_local
        run_local()
        return
    with Session.begin() as db:
        if args.command == "generate-templates":
            generate_templates(db)
        elif args.command == "setup-telegram":
            telegram("setWebhook", {"url": settings().public_url + "/telegram/webhook", "secret_token": settings().telegram_webhook_secret, "allowed_updates": ["message", "edited_message", "callback_query", "my_chat_member", "chat_member"]})
            message = telegram("sendMessage", {"chat_id": settings().telegram_channel_id, "text": render(db, "welcome"), "reply_markup": welcome_buttons()})
            telegram("pinChatMessage", {"chat_id": settings().telegram_channel_id, "message_id": message["message_id"]})
        elif args.command == "dead-jobs":
            for job in db.scalars(select(Job).where(Job.status == "dead")):
                print(json.dumps({"id": job.id, "kind": job.kind, "error": job.error}))
        elif args.command == "retry":
            job = db.get(Job, args.id)
            if not job or job.status != "dead":
                parser.error("Provide a dead job ID")
            job.status, job.attempts, job.due_at = "pending", 0, time.time()
        elif args.command == "close-ride":
            if args.actor not in settings().operator_ids or len(args.reason.strip()) < 10:
                parser.error("An allowlisted operator and descriptive reason are required")
            service = Rides(db)
            ride = db.get(Ride, args.id)
            if not ride or ride.state != "Trip_Started":
                parser.error("Only a stuck started trip can be administratively closed")
            users = service.users(ride.rider_id, ride.driver_id)
            ride = service.ride(ride.id)
            ride.state, ride.ended_at = "Administrative_Closure", time.time()
            for user in users.values():
                user.active_ride = None
            db.add(Event(ride_id=ride.id, actor_id=args.actor, action="operator_close", note=args.reason[:255]))
            service.notify(ride, "operator_close", args.actor)
        elif args.command == "export-training":
            from pathlib import Path
            target = Path(args.output)
            if "private-data" not in target.parts:
                parser.error("Exports must be placed under ignored private-data/")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8") as stream:
                for sample in db.scalars(select(TrainingSample).where(TrainingSample.eligible.is_(True), TrainingSample.completed_at > time.time() - 365 * 86400)):
                    stream.write(json.dumps({"distance_metres": sample.distance_metres, "price_cents": sample.agreed_price_cents, "currency": "CAD", "city": sample.pickup_city, "completed_at": sample.completed_at}) + "\n")

