"""Verify supervisor routing, bounded agent tools, and safe template generation."""

import json
from types import SimpleNamespace

import pytest
from conftest import NOW

from servicehub.agents import runtime
from servicehub.integrations.telegram import messaging
from servicehub.rides.domain import Rides


class FakeCall:
    """Represent the function-call fields consumed by the agent loop."""

    type = "function_call"

    def __init__(self, name: str, arguments: dict, call_id: str = "call-1"):
        """Store a tool name, JSON arguments, and stable call identifier."""

        self.name = name
        self.arguments = json.dumps(arguments)
        self.call_id = call_id

    def model_dump(self, exclude_none=True):
        """Return the Responses API item shape appended to model context."""

        del exclude_none
        return {
            "type": self.type,
            "name": self.name,
            "arguments": self.arguments,
            "call_id": self.call_id,
        }


class FakeResponses:
    """Return a configured sequence of small Responses API objects."""

    def __init__(self, responses):
        """Store responses and all submitted request keyword arguments."""

        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        """Record a request and return the next configured response."""

        self.calls.append(kwargs)
        return self.responses.pop(0)


def response(*, calls=None, text=""):
    """Create a minimal object compatible with Responses API consumption."""

    return SimpleNamespace(output=calls or [], output_text=text)


def configured_settings(api_key="test-key"):
    """Return settings required by agent and template code."""

    return SimpleNamespace(openai_api_key=api_key, openai_model="test-model", environment="local")


def test_supervisor_explicit_fallback_structured_route_and_clarification(monkeypatch):
    """Prefer explicit routes, fall back without a key, and parse bounded structured classification."""

    assert runtime.supervisor("anything", "ride") == "ride"
    monkeypatch.setattr(runtime, "settings", lambda: configured_settings(""))
    assert runtime.supervisor("ambiguous") == "lili"
    fake = FakeResponses([response(text='{"service":"ride"}'), response(text='{"service":"clarify"}')])
    monkeypatch.setattr(runtime, "settings", configured_settings)
    monkeypatch.setattr(runtime, "client", lambda: SimpleNamespace(responses=fake))
    assert runtime.supervisor("book a ride") == "ride"
    assert runtime.supervisor("unclear words") == "clarify"


def test_agent_no_key_fallback_and_clarification(db, user_factory, monkeypatch):
    """Return safe deterministic guidance when OpenAI is unavailable or intent is unclear."""

    user_factory()
    service = Rides(db, NOW)
    monkeypatch.setattr(runtime, "settings", lambda: configured_settings(""))
    fallback = runtime.answer(service, 101, "help")
    assert fallback["open_form"] is True
    monkeypatch.setattr(runtime, "settings", configured_settings)
    monkeypatch.setattr(runtime, "supervisor", lambda *_args: "clarify")
    clarification = runtime.answer(service, 101, "something")
    assert "request/manage a ride" in clarification["text"]


@pytest.mark.parametrize(
    ("tool_name", "tool_args", "expected_key"),
    [
        ("current_ride", {}, "commands"),
        ("open_ride_form", {}, "open_form"),
        ("prepare_action", {"action": "cancel"}, "commands"),
    ],
)
def test_ride_agent_tools_read_open_and_prepare_without_confirming(
    db, user_factory, ride_factory, monkeypatch, tool_name, tool_args, expected_key
):
    """Execute supported ride tools while leaving every mutation behind explicit confirmation."""

    user = user_factory()
    ride = ride_factory(rider_id=user.id, state="Open", bid_until=NOW + 300, choose_until=NOW + 600)
    user.active_ride = ride.id
    fake = FakeResponses(
        [
            response(calls=[FakeCall(tool_name, tool_args)]),
            response(text="I prepared the requested next step."),
        ]
    )
    monkeypatch.setattr(runtime, "settings", configured_settings)
    monkeypatch.setattr(runtime, "supervisor", lambda *_args: "ride")
    monkeypatch.setattr(runtime, "guide", lambda: "Use the verified ride form and explicit confirmations.")
    monkeypatch.setattr(runtime, "client", lambda: SimpleNamespace(responses=fake))
    result = runtime.answer(Rides(db, NOW), user.id, "Please help")
    assert result["text"] == "I prepared the requested next step."
    if tool_name == "open_ride_form":
        assert result[expected_key] is True
    elif tool_name == "prepare_action":
        assert len(result[expected_key]) == 1
        assert ride.state == "Open"
    else:
        assert result[expected_key] == []


def test_agent_rejects_unavailable_tool_and_stops_after_four_iterations(
    db, user_factory, monkeypatch
):
    """Return a deterministic fallback after four tool rounds without executing unavailable operations."""

    user_factory(context={"history": [{"role": "user", "content": str(index)} for index in range(12)]})
    fake = FakeResponses(
        [response(calls=[FakeCall("delete_everything", {}, f"call-{index}")]) for index in range(4)]
    )
    monkeypatch.setattr(runtime, "settings", configured_settings)
    monkeypatch.setattr(runtime, "supervisor", lambda *_args: "ride")
    monkeypatch.setattr(runtime, "guide", lambda: "Ride guide")
    monkeypatch.setattr(runtime, "client", lambda: SimpleNamespace(responses=fake))
    result = runtime.answer(Rides(db, NOW), 101, "keep trying")
    assert result == {
        "text": "Please use the ride form or My Ride to continue.",
        "open_form": True,
        "commands": [],
    }
    assert len(fake.calls) == 4
    assert [item["content"] for item in fake.calls[0]["input"][:8]] == [str(index) for index in range(4, 12)]


def test_lili_exposes_only_read_only_tools(db, user_factory, monkeypatch):
    """Prevent the general service agent from opening forms or proposing ride mutations."""

    user_factory()
    fake = FakeResponses(
        [response(calls=[FakeCall("prepare_action", {"action": "cancel"})]), response(text="That tool is unavailable.")]
    )
    monkeypatch.setattr(runtime, "settings", configured_settings)
    monkeypatch.setattr(runtime, "supervisor", lambda *_args: "lili")
    monkeypatch.setattr(runtime, "guide", lambda: "Ride guide")
    monkeypatch.setattr(runtime, "client", lambda: SimpleNamespace(responses=fake))
    result = runtime.answer(Rides(db, NOW), 101, "Cancel something")
    assert result["commands"] == []
    assert result["open_form"] is False


def test_template_generation_preserves_placeholders(monkeypatch, db):
    """Persist generated templates only when all exact placeholders survive rewriting."""

    class EchoResponses:
        """Echo each baseline inside the required JSON envelope."""

        def create(self, **kwargs):
            """Return the submitted baseline as generated template text."""

            return response(text=json.dumps({"text": kwargs["input"]}))

    monkeypatch.setattr(messaging, "settings", configured_settings)
    monkeypatch.setattr(messaging, "client", lambda: SimpleNamespace(responses=EchoResponses()))
    messaging.generate_templates(db)
    db.flush()
    assert db.get(messaging.Template, "request").text == messaging.TEMPLATES["request"]


@pytest.mark.parametrize("generated", ["missing required fields", "x" * 1001])
def test_template_generation_rejects_malformed_or_overlong_output(monkeypatch, db, generated):
    """Reject generated prose that drops placeholders or exceeds Telegram-safe cache limits."""

    outputs = [response(text=json.dumps({"text": generated}))]
    if generated == "missing required fields":
        outputs = []
        for baseline in messaging.TEMPLATES.values():
            if messaging.placeholders(baseline):
                outputs.append(response(text=json.dumps({"text": generated})))
                break
            outputs.append(response(text=json.dumps({"text": baseline})))
    fake = FakeResponses(outputs)
    monkeypatch.setattr(messaging, "settings", configured_settings)
    monkeypatch.setattr(messaging, "client", lambda: SimpleNamespace(responses=fake))
    with pytest.raises(ValueError, match="failed validation"):
        messaging.generate_templates(db)
