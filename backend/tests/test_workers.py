"""Verify durable job leasing, retries, typed execution, delivery guards, and dispatch."""

from types import SimpleNamespace

import pytest
from conftest import NOW
from sqlalchemy import select

from servicehub.database.tables import Job
from servicehub.integrations.providers import ProviderError
from servicehub.workers import jobs


def test_run_job_claims_completes_and_skips_ineligible_work(
    db, job_factory, monkeypatch
):
    """Claim due jobs once and skip future, actively leased, done, and dead records."""

    due = job_factory(dedupe="due")
    future = job_factory(dedupe="future", due_at=NOW + 1)
    leased = job_factory(dedupe="leased", lease_until=NOW + 1)
    done = job_factory(dedupe="done", status="done")
    dead = job_factory(dedupe="dead", status="dead")
    db.commit()
    calls = []
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)
    monkeypatch.setattr(jobs, "execute", lambda kind, payload, job_id: calls.append((kind, payload, job_id)))
    for row in (due, future, leased, done, dead):
        jobs.run_job(row.id)
    db.expire_all()
    assert calls == [(due.kind, due.payload, due.id)]
    assert db.get(Job, due.id).status == "done"
    assert db.get(Job, due.id).attempts == 1
    assert db.get(Job, due.id).lease_token is None
    assert db.get(Job, future.id).attempts == 0


@pytest.mark.parametrize(
    ("failure", "attempts", "expected", "delay"),
    [
        (ProviderError("telegram", retry_after=17), 0, "pending", 17),
        (ProviderError("telegram", permanent=True), 0, "dead", 30),
        (RuntimeError("unexpected"), 7, "dead", 256),
    ],
)
def test_run_job_retry_and_dead_letter_policy(
    db, job_factory, monkeypatch, failure, attempts, expected, delay
):
    """Reschedule transient failures and dead-letter permanent or exhausted work."""

    job = job_factory(attempts=attempts)
    db.commit()
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)

    def fail(*_args):
        """Raise the configured provider or generic worker failure."""

        raise failure

    monkeypatch.setattr(jobs, "execute", fail)
    jobs.run_job(job.id)
    db.expire_all()
    stored = db.get(Job, job.id)
    assert stored.status == expected
    assert stored.due_at == NOW + delay
    expected_error = str(failure) if isinstance(failure, ProviderError) else "RuntimeError"
    assert stored.error == expected_error[:128]


def test_run_job_truncates_errors_and_protects_replaced_lease(db, job_factory, monkeypatch):
    """Bound persisted error text and prevent stale workers from acknowledging another lease."""

    long_error = job_factory(dedupe="long-error")
    replaced = job_factory(dedupe="replaced")
    db.commit()
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)
    monkeypatch.setattr(
        jobs,
        "execute",
        lambda *_args: (_ for _ in ()).throw(ProviderError("telegram", detail="x" * 300)),
    )
    jobs.run_job(long_error.id)
    db.expire_all()
    assert len(db.get(Job, long_error.id).error) == 128

    def replace_lease(_kind, _payload, job_id):
        """Simulate a second worker taking over before acknowledgement."""

        with jobs.Session.begin() as session:
            session.get(Job, job_id).lease_token = "replacement"

    monkeypatch.setattr(jobs, "execute", replace_lease)
    jobs.run_job(replaced.id)
    db.expire_all()
    assert db.get(Job, replaced.id).status == "running"
    assert db.get(Job, replaced.id).lease_token == "replacement"


def test_execute_telegram_and_send_jobs_with_revision_and_state_guards(
    db, user_factory, ride_factory, monkeypatch
):
    """Dispatch Telegram updates and drop stale or terminal delivery payloads."""

    from servicehub.integrations.telegram import handlers

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(rider_id=rider.id, driver_id=driver.id, state="Driver_En_Route", revision=3)
    db.commit()
    updates = []
    telegram_calls = []
    monkeypatch.setattr(handlers, "handle_update", lambda payload: updates.append(payload))
    monkeypatch.setattr(jobs, "telegram", lambda method, payload: telegram_calls.append((method, payload)) or {})
    jobs.execute("telegram_update", {"update_id": 1}, "inbox")
    jobs.execute("send", {"chat_id": 101, "text": "hello"}, "send")
    jobs.execute(
        "send",
        {"chat_id": 101, "text": "stale", "_ride_id": ride.id, "_revision": 2},
        "stale",
    )
    jobs.execute(
        "send_location",
        {"chat_id": 101, "latitude": 48.4, "longitude": -89.25, "ride_id": ride.id},
        "location",
    )
    ride.state = "Completed"
    db.commit()
    jobs.execute(
        "send_location",
        {"chat_id": 101, "latitude": 48.4, "longitude": -89.25, "ride_id": ride.id},
        "terminal-location",
    )
    assert updates == [{"update_id": 1}]
    assert [method for method, _payload in telegram_calls] == ["sendMessage", "sendLocation"]


def test_execute_direct_offer_expiry_and_pickup_jobs(
    db, user_factory, ride_factory, offer_factory, monkeypatch
):
    """Fan out direct and offer deliveries and apply scheduled ride-state transitions."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=NOW,
        choose_until=NOW + 300,
    )
    rider.active_ride = ride.id
    offer = offer_factory(ride, driver.id)
    db.commit()
    monkeypatch.setattr(jobs.time, "time", lambda: NOW + 1)
    jobs.execute("direct", {"chat_id": rider.id, "key": "success", "values": {}}, "direct-1")
    jobs.execute("offer_event", {"offer_id": offer.id, "revision": offer.revision}, "offer-1")
    jobs.execute("expiry", {"ride_id": ride.id, "generation": ride.generation}, "expiry-1")
    db.expire_all()
    ride = db.get(type(ride), ride.id)
    assert ride.state == "Selecting"
    delivery_jobs = db.scalars(select(Job).where(Job.kind == "send")).all()
    assert len(delivery_jobs) == 2

    ride.state = "Pickup_Confirmation_Pending"
    ride.driver_id = driver.id
    ride.pickup_attempt = "attempt-1"
    ride.pickup_until = NOW
    db.commit()
    jobs.execute(
        "pickup_expiry",
        {"ride_id": ride.id, "attempt": "attempt-1"},
        "pickup-expiry",
    )
    db.expire_all()
    assert db.get(type(ride), ride.id).state == "Driver_En_Route"


def test_ride_event_and_reminder_fan_out_are_version_guarded(
    db, user_factory, ride_factory
):
    """Create deduplicated participant and channel outbox work only for current revisions."""

    rider = user_factory()
    driver = user_factory(202, "Driver")
    ride = ride_factory(
        rider_id=rider.id,
        driver_id=driver.id,
        state="Matched",
        revision=5,
        price_cents=2600,
    )
    db.commit()
    jobs.execute(
        "ride_event",
        {"ride_id": ride.id, "revision": 4, "action": "matched"},
        "stale-event",
    )
    assert db.scalars(select(Job)).all() == []
    jobs.execute(
        "ride_event",
        {"ride_id": ride.id, "revision": 5, "action": "matched"},
        "current-event",
    )
    jobs.execute(
        "reminder",
        {"ride_id": ride.id, "generation": ride.generation},
        "current-reminder",
    )
    db.expire_all()
    rows = db.scalars(select(Job).order_by(Job.dedupe)).all()
    assert sum(row.kind == "send" for row in rows) == 4
    assert sum(row.kind == "channel" for row in rows) == 1
    assert len({row.dedupe for row in rows}) == len(rows)
    ride.generation += 1
    db.commit()
    before = len(rows)
    jobs.execute("reminder", {"ride_id": ride.id, "generation": 1}, "stale-reminder")
    db.expire_all()
    assert len(db.scalars(select(Job)).all()) == before


def test_update_channel_creates_then_edits_latest_public_markdown(
    db, user_factory, ride_factory, monkeypatch
):
    """Create an open-ride post, then edit it without buttons or private address details."""

    rider = user_factory()
    ride = ride_factory(
        rider_id=rider.id,
        state="Open",
        bid_until=NOW + 300,
        choose_until=NOW + 600,
    )
    db.commit()
    calls = []

    def telegram(method, payload):
        """Capture channel requests and return a message identifier for creation."""

        calls.append((method, payload))
        return {"message_id": 77}

    monkeypatch.setattr(jobs, "telegram", telegram)
    jobs.update_channel(ride.id)
    db.expire_all()
    assert db.get(type(ride), ride.id).channel_message == 77
    method, body = calls[-1]
    assert method == "sendMessage"
    assert body["parse_mode"] == "MarkdownV2"
    assert f"Ride ID: *{ride.id[:8].upper()}*" in body["text"]
    assert "4B" not in body["text"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["text"] == "Make an Offer"
    ride.state = "Expired"
    db.commit()
    jobs.update_channel(ride.id)
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["message_id"] == 77
    assert calls[-1][1]["reply_markup"] == {"inline_keyboard": []}


def test_dispatch_constructs_cloud_task_request(db, job_factory, monkeypatch):
    """Build Cloud Tasks requests with the configured queue, worker URL, OIDC account, and audience."""

    from google.cloud import tasks_v2

    job = job_factory()
    job_factory(dedupe="future", due_at=NOW + 1)
    job_factory(dedupe="leased", lease_until=NOW + 1)
    db.commit()
    created = []

    class FakeCloudTasksClient:
        """Capture queue-path and task creation calls."""

        def queue_path(self, project, location, queue):
            """Return a recognizable fully qualified queue path."""

            return f"projects/{project}/locations/{location}/queues/{queue}"

        def create_task(self, **kwargs):
            """Store one requested Cloud Task."""

            created.append(kwargs)

    config = SimpleNamespace(
        gcp_project="project",
        gcp_location="northamerica-northeast2",
        task_queue="servicehub",
        worker_url="https://worker.test",
        worker_service_account="worker@project.iam.gserviceaccount.com",
        internal_audience="https://worker.test",
    )
    monkeypatch.setattr(tasks_v2, "CloudTasksClient", FakeCloudTasksClient)
    monkeypatch.setattr(jobs, "settings", lambda: config)
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)
    jobs.dispatch()
    assert len(created) == 1
    request = created[0]
    assert request["parent"].endswith("queues/servicehub")
    http_request = request["task"]["http_request"]
    assert http_request["url"] == f"https://worker.test/internal/jobs/{job.id}"
    assert http_request["oidc_token"] == {
        "service_account_email": config.worker_service_account,
        "audience": config.internal_audience,
    }


def test_run_local_performs_one_poll_with_daily_cleanup_deduplication(
    db, job_factory, monkeypatch
):
    """Select only due unleased jobs and enqueue one cleanup record during a single local poll."""

    due = job_factory("send", dedupe="due")
    job_factory("send", dedupe="future", due_at=NOW + 1)
    job_factory("send", dedupe="leased", lease_until=NOW + 1)
    db.commit()
    selected = []
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)
    monkeypatch.setattr(jobs, "run_job", lambda job_id: selected.append(job_id))

    def stop(_seconds):
        """End the infinite worker loop after its first completed iteration."""

        raise StopIteration

    monkeypatch.setattr(jobs.time, "sleep", stop)
    with pytest.raises(StopIteration):
        jobs.run_local()
    db.expire_all()
    cleanup = db.scalars(select(Job).where(Job.kind == "cleanup")).all()
    assert len(cleanup) == 1
    assert set(selected) == {due.id, cleanup[0].id}
