"""Approvals, Questions and handoffs: request shapes, and the three wait loops.

The loops share one implementation, so the loop tests run the same scenarios over
all three (answer arrives, outage bounds, 429 with Retry-After, cancel, onRetry,
malformed answers). Expectations come from the TypeScript SDK's wait-loops tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from conftest import START, FakeClock, Harness, Recorder, iso, reply, static
from pingroom import PingRoomError, PingRoomTimeoutError
from pingroom._retry import (
    TRANSIENT_BACKOFF_CAP,
    WAIT_EXPIRY_GRACE,
    WAIT_UNKNOWN_EXPIRY_OUTAGE,
    is_transient_error,
    transient_delay,
)

MINUTE = 60.0


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


def test_approvals_request_posts_to_the_room_with_the_idempotency_header(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "a1", "status": "pending"}, 201))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.approvals.request(
            "AB12",
            "Ship release 1.4.0?",
            title="Release",
            options=["Ship it", "Hold"],
            correlation_id="deploy-1.4.0",
            data={"v": 1},
            ttl=600,
            idempotency_key="deploy-1.4.0-approval",
        ),
    )
    assert rec.last.method == "POST"
    assert rec.last.url.path == "/api/agent/rooms/AB12/approvals"
    assert rec.body() == {
        "question": "Ship release 1.4.0?",
        "title": "Release",
        "options": ["Ship it", "Hold"],
        "correlation_id": "deploy-1.4.0",
        "data": {"v": 1},
        "ttl": 600,
    }
    assert rec.last.headers["idempotency-key"] == "deploy-1.4.0-approval"


def test_approvals_request_minimal_body_and_no_key(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "a1"}, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.approvals.request("AB12", "Ship it?"))
    assert rec.body() == {"question": "Ship it?"}
    assert "idempotency-key" not in rec.last.headers


def test_approvals_get_and_wait(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "a1", "status": "pending"}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.approvals.get("a1"))
    assert (rec.last.method, rec.last.url.path) == ("GET", "/api/agent/approvals/a1")
    harness.run(client, lambda c: c.approvals.wait("a1", timeout=5))
    assert rec.last.url.path == "/api/agent/approvals/a1/wait"
    assert rec.query() == {"timeout": "5"}
    assert rec.last.extensions["timeout"]["read"] == 15
    harness.run(client, lambda c: c.approvals.wait("a1"))
    assert rec.last.url.query == b""
    assert rec.last.extensions["timeout"]["read"] == 30


def test_questions_ask_posts_the_body_to_the_room(harness: Harness) -> None:
    rec = Recorder(
        fallback=static({"id": "q1", "kind": "question", "state": "pending", "answer": None}, 201)
    )
    client = harness.client(rec)
    question = harness.run(
        client,
        lambda c: c.questions.ask(
            "ab12",
            "Deploy?",
            options=["ship", "hold"],
            responder_scope="room",
            ttl=600,
            correlation_id="d-1",
            attachment_ids=["att_1", "att_2", "att_3", "att_4"],
        ),
    )
    assert question["id"] == "q1"
    assert rec.last.url.path == "/api/agent/rooms/ab12/questions"
    assert rec.body() == {
        "prompt": "Deploy?",
        "options": ["ship", "hold"],
        "responder_scope": "room",
        "ttl": 600,
        "correlation_id": "d-1",
        "attachment_ids": ["att_1", "att_2", "att_3", "att_4"],
    }


def test_questions_ask_every_field(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "q1"}, 201))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.questions.ask(
            "ab12",
            "Which?",
            context="Build 41",
            options=["a", {"value": "b", "label": "Bee", "style": "primary"}],
            text_input={"placeholder": "Other?", "max_length": 30},
            responder_scope="direct",
            resolution_policy="first",
            target_user_id="0190a2b4-0000-7000-8000-000000000001",
            ttl=300,
            data={"k": 1},
            correlation_id="c",
            reply_to="r",
            attachment_ids=["x"],
            idempotency_key="idem",
        ),
    )
    assert rec.body() == {
        "prompt": "Which?",
        "context": "Build 41",
        "options": ["a", {"value": "b", "label": "Bee", "style": "primary"}],
        "text_input": {"placeholder": "Other?", "max_length": 30},
        "responder_scope": "direct",
        "resolution_policy": "first",
        "target_user_id": "0190a2b4-0000-7000-8000-000000000001",
        "ttl": 300,
        "data": {"k": 1},
        "correlation_id": "c",
        "reply_to": "r",
        "attachment_ids": ["x"],
    }
    assert rec.last.headers["idempotency-key"] == "idem"


def test_a_poll_sends_resolution_policy_and_returns_the_tally_untouched(harness: Harness) -> None:
    results = {
        "audience_count": 3,
        "response_count": 2,
        "options": [
            {"value": "pizza", "label": "Pizza", "count": 2},
            {"value": "tacos", "label": "Tacos", "count": 0},
        ],
        "responses": [],
        "viewer": None,
    }
    body = {
        "id": "q1",
        "kind": "question",
        "state": "answered",
        "resolution_policy": "all",
        "answer": None,
        "results": results,
    }
    rec = Recorder(fallback=static(body))
    client = harness.client(rec)
    poll = harness.run(
        client,
        lambda c: c.questions.ask(
            "AB12", "Lunch?", options=["pizza", "tacos"], resolution_policy="all", ttl=600
        ),
    )
    assert rec.body() == {
        "prompt": "Lunch?",
        "options": ["pizza", "tacos"],
        "resolution_policy": "all",
        "ttl": 600,
    }
    assert poll["answer"] is None
    assert poll["results"] == results
    assert harness.run(client, lambda c: c.questions.wait_for_answer("q1")) == body


@pytest.mark.parametrize(
    "extra",
    [
        {"responder_scope": "direct"},
        {"target_user_id": "0190a2b4-0000-7000-8000-000000000001"},
        {"text_input": {"placeholder": "Other?"}},
    ],
)
def test_a_poll_combined_with_a_direct_scope_a_target_or_typed_answer_fails_before_sending(
    harness: Harness, extra: Dict[str, Any]
) -> None:
    rec = Recorder(fallback=static({"id": "q1"}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(
            client,
            lambda c: c.questions.ask(
                "AB12", "Lunch?", options=["pizza", "tacos"], resolution_policy="all", **extra
            ),
        )
    assert caught.value.code == "invalid_request"
    assert rec.requests == []
    # A room-scope poll, and a first-answer question with the same extras, are left to the server.
    harness.run(
        client,
        lambda c: c.questions.ask(
            "AB12", "Lunch?", resolution_policy="all", responder_scope="room"
        ),
    )
    harness.run(
        client,
        lambda c: c.questions.ask(
            "AB12", "Why?", resolution_policy="first", text_input={"placeholder": "Reason"}
        ),
    )
    assert len(rec.requests) == 2


def test_questions_list_unwraps_and_sends_the_state_filter(harness: Harness) -> None:
    rec = Recorder(
        fallback=static({"questions": [{"id": "q1", "kind": "question", "state": "answered"}]})
    )
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.questions.list(state="answered"))
    assert [q["id"] for q in result] == ["q1"]
    assert rec.query() == {"state": "answered"}
    client = harness.client(static({}))
    assert harness.run(client, lambda c: c.questions.list()) == []


def test_questions_cancel_posts_an_empty_object(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "q1", "state": "cancelled"}))
    client = harness.client(rec)
    question = harness.run(client, lambda c: c.questions.cancel("q1"))
    assert question["state"] == "cancelled"
    assert (rec.last.method, rec.last.url.path) == ("POST", "/api/agent/questions/q1/cancel")
    assert rec.last.content == b"{}"


def test_questions_get_and_wait(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "q1", "state": "pending"}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.questions.get("q1"))
    assert rec.last.url.path == "/api/agent/questions/q1"
    harness.run(client, lambda c: c.questions.wait("q1", timeout=0))
    assert rec.last.url.path == "/api/agent/questions/q1/wait"
    assert rec.query() == {"timeout": "0"}


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.approvals.request("AB12", "q", data={"actions": [{"label": "x"}]}),
        lambda c: c.questions.ask("AB12", "q", data={"actions": [{"label": "x"}]}),
        lambda c: c.handoffs.request_ack("q", data={"actions": [{"label": "x"}]}),
        lambda c: c.handoffs.ask("q", ["a", "b"], data={"actions": [{"label": "x"}]}),
    ],
)
def test_the_doors_that_never_carry_buttons_refuse_data_actions(
    harness: Harness, call: Callable[[Any], Any]
) -> None:
    rec = Recorder(fallback=static({"id": "x"}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, call)
    assert caught.value.code == "invalid_data"
    assert "cannot carry buttons" in caught.value.message
    assert rec.requests == []


@pytest.mark.parametrize("key", ["", "   ", "k" * 256, "✅", 42])
def test_approvals_and_questions_refuse_a_bad_key_before_sending(
    harness: Harness, key: Any
) -> None:
    rec = Recorder(fallback=static({"id": "x"}))
    client = harness.client(rec)
    for call in (
        lambda c: c.approvals.request("AB12", "Ship?", idempotency_key=key),
        lambda c: c.questions.ask("AB12", "Ship?", idempotency_key=key),
    ):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, call)
        assert caught.value.code == "invalid_request"
        assert "idempotency_key" in caught.value.message
    assert rec.requests == []


# --- handoffs --------------------------------------------------------------


def test_handoffs_request_ack_posts_a_direct_ack_handoff(harness: Harness) -> None:
    rec = Recorder(
        fallback=static(
            {
                "id": "h1",
                "kind": "ack",
                "prompt": "Ack me",
                "state": "open",
                "delivery_state": "enqueued",
            },
            201,
        )
    )
    client = harness.client(rec)
    handoff = harness.run(
        client,
        lambda c: c.handoffs.request_ack(
            "Ack me",
            expires_in=600,
            correlation_id="deploy-1",
            data={"commit": "abc"},
            idempotency_key="idem-1",
        ),
    )
    assert handoff["delivery_state"] == "enqueued"
    assert rec.last.url.path == "/api/agent/handoffs"
    assert rec.last.headers["idempotency-key"] == "idem-1"
    assert rec.body() == {
        "kind": "ack",
        "prompt": "Ack me",
        "audience": {"type": "direct", "user_id": "me"},
        "expires_in": 600,
        "correlation_id": "deploy-1",
        "data": {"commit": "abc"},
    }


def test_handoffs_request_ack_every_field_and_an_explicit_target(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "h2", "kind": "ack", "state": "open"}, 201))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.handoffs.request_ack(
            "hi", target="user-uuid-9", urgency="passive", reply_to="r", expires_in=900
        ),
    )
    assert "idempotency-key" not in rec.last.headers
    assert rec.body() == {
        "kind": "ack",
        "prompt": "hi",
        "audience": {"type": "direct", "user_id": "user-uuid-9"},
        "expires_in": 900,
        "urgency": "passive",
        "reply_to": "r",
    }


def test_handoffs_ask_normalises_bare_string_options(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "h3", "kind": "question", "state": "pending"}, 201))
    client = harness.client(rec)
    handoff = harness.run(
        client,
        lambda c: c.handoffs.ask("Ship?", ["deploy", {"value": "hold", "style": "danger"}]),
    )
    assert handoff["kind"] == "question"
    assert rec.body()["kind"] == "question"
    assert rec.body()["options"] == [
        {"value": "deploy", "label": "deploy"},
        {"value": "hold", "style": "danger"},
    ]


@pytest.mark.parametrize("options", [["only-one"], ["1", "2", "3", "4", "5"], [], None, "ab"])
def test_handoffs_ask_needs_two_to_four_options_before_fetching(
    harness: Harness, options: Any
) -> None:
    rec = Recorder(fallback=static({"id": "h"}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.handoffs.ask("x", options))
    assert caught.value.code == "invalid_request"
    assert "between 2 and 4" in caught.value.message
    assert rec.requests == []


def test_handoff_keys_are_validated_but_an_empty_one_means_no_key(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "h", "kind": "ack", "state": "open"}, 201))
    client = harness.client(rec)
    for key in ("✅", "k" * 256, "   "):
        for call in (
            lambda c, k=key: c.handoffs.request_ack("Ship?", idempotency_key=k),
            lambda c, k=key: c.handoffs.ask("Ship?", ["a", "b"], idempotency_key=k),
        ):
            with pytest.raises(PingRoomError) as caught:
                harness.run(client, call)
            assert caught.value.code == "invalid_request"
    assert rec.requests == []
    harness.run(client, lambda c: c.handoffs.request_ack("Ship?", idempotency_key=" deploy 42 "))
    assert rec.last.headers["idempotency-key"] == "deploy 42"
    harness.run(client, lambda c: c.handoffs.request_ack("Ship?", idempotency_key=""))
    assert "idempotency-key" not in rec.last.headers


def test_handoffs_get_and_list(harness: Harness) -> None:
    rec = Recorder(fallback=static({"handoffs": [{"id": "h5", "kind": "ack", "state": "open"}]}))
    client = harness.client(rec)
    handoffs = harness.run(client, lambda c: c.handoffs.list(state="all"))
    assert [h["id"] for h in handoffs] == ["h5"]
    assert rec.query() == {"state": "all"}
    harness.run(client, lambda c: c.handoffs.get("h5"))
    assert rec.last.url.path == "/api/agent/handoffs/h5"
    client = harness.client(static({}))
    assert harness.run(client, lambda c: c.handoffs.list()) == []


def test_a_handoff_idempotency_conflict_is_a_typed_error(harness: Harness) -> None:
    client = harness.client(static({"code": "idempotency_conflict", "message": "reused key"}, 409))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.handoffs.request_ack("hi", idempotency_key="dup"))
    assert (caught.value.status, caught.value.code) == (409, "idempotency_conflict")


# ---------------------------------------------------------------------------
# The wait loops
# ---------------------------------------------------------------------------


@dataclass
class Loop:
    name: str
    what: str
    pending: Callable[[Optional[str]], Dict[str, Any]]
    done: Dict[str, Any]
    run: Callable[[Any, Dict[str, Any]], Any]
    path: str
    state_field: str


def _approval_pending(expires_at: Optional[str]) -> Dict[str, Any]:
    return {
        "id": "appr-1",
        "status": "pending",
        "decision": None,
        "decided_at": None,
        "expires_at": expires_at,
    }


LOOPS = [
    Loop(
        "approvals.wait_for_decision",
        "approval appr-1",
        _approval_pending,
        {
            "id": "appr-1",
            "status": "decided",
            "decision": "approve",
            "decided_at": "2026-10-27T10:00:00+00:00",
            "expires_at": None,
        },
        lambda c, o: c.approvals.wait_for_decision("appr-1", **o),
        "/api/agent/approvals/appr-1/wait",
        "status",
    ),
    Loop(
        "questions.wait_for_answer",
        "question q-1",
        lambda expires_at: {
            "id": "q-1",
            "kind": "question",
            "state": "pending",
            "answer": None,
            "expires_at": expires_at,
        },
        {
            "id": "q-1",
            "kind": "question",
            "state": "answered",
            "answer": {"value": "ship", "label": "Ship"},
            "expires_at": None,
        },
        lambda c, o: c.questions.wait_for_answer("q-1", **o),
        "/api/agent/questions/q-1/wait",
        "state",
    ),
    Loop(
        "handoffs.wait_for_result",
        "handoff h-1",
        lambda expires_at: {"id": "h-1", "kind": "ack", "state": "open", "expires_at": expires_at},
        {
            "id": "h-1",
            "kind": "ack",
            "state": "acked",
            "acked_at": "2026-10-27T10:00:00+00:00",
            "expires_at": None,
        },
        lambda c, o: c.handoffs.wait_for_result("h-1", **o),
        "/api/agent/handoffs/h-1/wait",
        "state",
    ),
]

loops = pytest.mark.parametrize("loop", LOOPS, ids=[loop.name for loop in LOOPS])

Step = Dict[str, Any]
Respond = Callable[[int, float], Step]

UNAVAILABLE_503: Step = {"status": 503, "text": "<html>Service Unavailable</html>"}


def always(step: Step) -> Respond:
    """Every request gets ``step``."""
    return lambda n, now: step


def first_then(first: Step, then: Step) -> Respond:
    """The first request gets ``first``, every later one ``then``."""
    return lambda n, now: first if n == 1 else then


def in_order(script: List[Step]) -> Respond:
    """The n-th request gets the n-th step; the last repeats."""
    return lambda n, now: script[min(n, len(script)) - 1]


class Transport:
    """Answers the n-th request (1-based) from ``respond(n, elapsed_seconds)``."""

    def __init__(self, respond: Respond, clock: FakeClock) -> None:
        self.respond = respond
        self.clock = clock
        self.times: List[float] = []
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock.elapsed())
        step = self.respond(len(self.requests), self.clock.elapsed())
        if "throws" in step:
            raise httpx.ConnectError("connection dropped", request=request)
        if "text" in step:
            return reply(text=step["text"], status=step.get("status", 200))
        return reply(step.get("body"), step.get("status", 200), step.get("headers"))


def test_the_outage_bounds_match_the_cli() -> None:
    assert WAIT_EXPIRY_GRACE == 5 * MINUTE
    assert WAIT_UNKNOWN_EXPIRY_OUTAGE == 15 * MINUTE


@loops
def test_a_pending_answer_is_polled_again_until_the_answer_arrives(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(
        lambda n, now: {"body": loop.pending(None) if n < 4 else loop.done}, clock
    )
    client = harness.client(transport)
    assert harness.run(client, lambda c: loop.run(c, {})) == loop.done
    assert len(transport.requests) == 4
    assert all(r.url.path == loop.path for r in transport.requests)
    # Pending answers are polled at least 2.1 s apart, and no longer than that.
    gaps = [round(b - a, 3) for a, b in zip(transport.times, transport.times[1:])]
    assert gaps == [2.1, 2.1, 2.1]


@loops
def test_a_terminal_answer_returns_at_once_without_the_poll_interval(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(lambda n, now: {"body": loop.done}, clock)
    client = harness.client(transport)
    assert harness.run(client, lambda c: loop.run(c, {})) == loop.done
    assert clock.elapsed() == 0
    assert len(transport.requests) == 1


@loops
def test_without_a_cancel_it_gives_up_after_15_minutes_of_unbroken_failures(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(lambda n, now: UNAVAILABLE_503, clock)
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    error = caught.value
    assert error.code == "unavailable"
    assert error.status == 0
    assert isinstance(error.__cause__, PingRoomError)
    assert error.__cause__.status == 503
    assert f"stopped waiting for {loop.what}" in error.message
    assert "(HTTP 503)" in error.message
    assert iso(START) in error.message
    assert 15 * MINUTE <= clock.elapsed() <= 15 * MINUTE + 2
    # When the next backoff will not fit, it stops at the bound without an early
    # retry or waiting one full backoff past it.
    assert clock.elapsed() - transport.times[-1] <= TRANSIENT_BACKOFF_CAP


@loops
def test_it_gives_up_at_the_last_seen_expires_at_plus_5_minutes_not_before(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    # Expiry 30 min out: the bound is 35 min, well past the 15-minute
    # unknown-expiry cap, so this proves the expiry is what is used.
    expires = START + 30 * MINUTE
    transport = Transport(
        lambda n, now: {"body": loop.pending(iso(expires))} if n == 1 else UNAVAILABLE_503, clock
    )
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    assert caught.value.code == "unavailable"
    assert expires + 5 * MINUTE - START <= clock.elapsed() <= expires + 5 * MINUTE - START + 2


@loops
def test_an_expiry_soon_gives_up_before_the_15_minute_cap(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    expires = START + 1 * MINUTE
    transport = Transport(
        lambda n, now: {"body": loop.pending(iso(expires))} if n == 1 else {"throws": True}, clock
    )
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    error = caught.value
    assert error.code == "unavailable"
    assert error.__cause__ is not None and error.__cause__.code == "network_error"  # type: ignore[attr-defined]
    assert "(network_error)" in error.message
    assert 6 * MINUTE <= clock.elapsed() <= 6 * MINUTE + 2


@loops
def test_the_outage_clock_restarts_after_any_valid_response(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    # 10 min of 503, one pending read with no expiry, 10 more min of 503, then
    # the outcome. No unbroken 15-minute outage, so the wait never gives up.
    state = {"recovered": False}

    def respond(n: int, now: float) -> Dict[str, Any]:
        if now < 10 * MINUTE:
            return UNAVAILABLE_503
        if not state["recovered"]:
            state["recovered"] = True
            return {"body": loop.pending(None)}
        if now < 21 * MINUTE:
            return UNAVAILABLE_503
        return {"body": loop.done}

    client = harness.client(Transport(respond, clock))
    assert harness.run(client, lambda c: loop.run(c, {})) == loop.done
    assert clock.elapsed() >= 21 * MINUTE


@loops
def test_on_retry_reports_each_retry_with_the_consecutive_attempt_number(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    script = [
        UNAVAILABLE_503,
        UNAVAILABLE_503,
        {"body": loop.pending(None)},
        UNAVAILABLE_503,
        {"body": loop.done},
    ]
    client = harness.client(Transport(lambda n, now: script[min(n, len(script)) - 1], clock))
    seen: List[Any] = []
    result = harness.run(
        client,
        lambda c: loop.run(
            c, {"on_retry": lambda error, attempt: seen.append((error.status, attempt))}
        ),
    )
    assert result == loop.done
    # The count restarts after the pending read in the middle.
    assert seen == [(503, 1), (503, 2), (503, 1)]


@loops
def test_an_on_retry_that_raises_ends_the_wait_with_that_error(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(lambda n, now: UNAVAILABLE_503, clock)
    client = harness.client(transport)
    stop = RuntimeError("three strikes")

    def on_retry(error: PingRoomError, attempt: int) -> None:
        if attempt == 3:
            raise stop

    with pytest.raises(RuntimeError) as caught:
        harness.run(client, lambda c: loop.run(c, {"on_retry": on_retry}))
    assert caught.value is stop
    assert len(transport.requests) == 3


@loops
def test_an_on_retry_that_is_not_callable_is_refused_before_sending(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(lambda n, now: {"body": loop.done}, clock)
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {"on_retry": "log"}))
    assert caught.value.code == "invalid_request"
    assert "on_retry" in caught.value.message
    assert transport.requests == []


@loops
def test_one_loop_stays_below_30_polls_a_minute_across_failures_and_pending_answers(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(
        lambda n, now: UNAVAILABLE_503 if n % 2 == 1 else {"body": loop.pending(None)}, clock
    )
    client = harness.client(transport)
    cancel = harness.new_cancel()
    clock.at(MINUTE, cancel.set)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {"cancel": cancel}))
    assert caught.value.code == "cancelled"
    assert clock.elapsed() == MINUTE
    assert len(transport.requests) == 29
    gaps = [b - a for a, b in zip(transport.times, transport.times[1:])]
    # (Fake time is a 1.79e9-second epoch, so allow float rounding of a microsecond.)
    assert all(2.1 - 1e-5 <= gap < 2.2 for gap in gaps), gaps


@loops
def test_cancelling_during_the_retry_interval_stops_without_another_request(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    transport = Transport(lambda n, now: UNAVAILABLE_503, clock)
    client = harness.client(transport)
    cancel = harness.new_cancel()
    clock.at(1.5, cancel.set)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {"cancel": cancel}))
    assert caught.value.code == "cancelled"
    assert clock.elapsed() == 1.5
    assert len(transport.requests) == 1


@loops
def test_it_stops_at_the_outage_deadline_without_an_early_retry(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    deadline = START + 2.5
    expiry = iso(deadline - WAIT_EXPIRY_GRACE)
    transport = Transport(
        lambda n, now: {"body": loop.pending(expiry)} if n == 1 else UNAVAILABLE_503, clock
    )
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    assert caught.value.code == "unavailable"
    assert clock.elapsed() == pytest.approx(2.5, abs=1e-4)
    assert transport.times == [0, 2.1]


@loops
def test_it_retries_at_the_outage_deadline_when_the_full_interval_fits(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    # 4.25 s: the retry at 2.1 s still has the whole 2.1 s interval left before the
    # deadline, so it is sent; the next one does not, so the wait ends at the deadline.
    deadline = START + 4.25
    expiry = iso(deadline - WAIT_EXPIRY_GRACE)
    transport = Transport(
        lambda n, now: {"body": loop.pending(expiry)} if n == 1 else UNAVAILABLE_503, clock
    )
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    assert caught.value.code == "unavailable"
    assert clock.elapsed() == pytest.approx(4.25, abs=1e-4)
    assert [round(t, 3) for t in transport.times] == [0, 2.1, 4.2]


@loops
def test_cancel_is_preserved_while_waiting_out_the_outage_budget(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    expiry = iso(START + 2.5 - WAIT_EXPIRY_GRACE)
    transport = Transport(
        lambda n, now: {"body": loop.pending(expiry)} if n == 1 else UNAVAILABLE_503, clock
    )
    client = harness.client(transport)
    cancel = harness.new_cancel()
    clock.at(2.3, cancel.set)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {"cancel": cancel}))
    assert caught.value.code == "cancelled"
    assert clock.elapsed() == pytest.approx(2.3, abs=1e-4)
    assert [round(t, 3) for t in transport.times] == [0, 2.1]


@loops
def test_a_429_is_retried_after_its_retry_after_from_the_header_or_the_body(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    limited: Dict[str, Any] = {"code": "rate_limited", "message": "Too Many Attempts."}
    steps: List[Step] = [
        {"status": 429, "body": limited, "headers": {"Retry-After": "1"}},
        {"status": 429, "body": limited, "headers": {"Retry-After": "3"}},
        {"status": 429, "body": {**limited, "retry_after": 3}},
        {"status": 429, "body": {**limited, "retry_after": 42}},
    ]
    for step in steps:
        clock.reset()
        transport = Transport(first_then(step, {"body": loop.done}), clock)
        client = harness.client(transport)
        assert harness.run(client, lambda c: loop.run(c, {})) == loop.done
        assert len(transport.requests) == 2
        header = step.get("headers", {}).get("Retry-After")
        expected = float(header) if header is not None else float(step["body"]["retry_after"])
        # A Retry-After longer than the 2.1 s poll floor is waited out in full; a shorter one
        # is lifted to the floor, because a failed poll still consumes one.
        assert transport.times[1] == max(expected, 2.1)


@loops
def test_a_5xx_and_a_dropped_connection_are_retried_with_backoff(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    for failure in (UNAVAILABLE_503, {"throws": True}):
        clock.reset()
        transport = Transport(first_then(failure, {"body": loop.done}), clock)
        client = harness.client(transport)
        assert harness.run(client, lambda c: loop.run(c, {})) == loop.done
        assert transport.times == [0, 2.1]  # the 1 s backoff lifted to the poll interval


@loops
@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_a_permanent_failure_is_raised_at_once(
    harness: Harness, clock: FakeClock, loop: Loop, status: int
) -> None:
    transport = Transport(
        lambda n, now: {"status": status, "body": {"message": f"HTTP {status}"}}, clock
    )
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: loop.run(c, {}))
    assert caught.value.status == status
    assert len(transport.requests) == 1


@loops
def test_a_malformed_2xx_is_never_returned_as_an_outcome(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    bodies: List[Any] = [
        {"text": "<html>captive portal</html>"},
        {"body": {}},
        {"body": []},
        {"body": {**loop.done, "id": "someone-else"}},
        {"body": {**loop.done, loop.state_field: "approved"}},
        {"body": {k: v for k, v in loop.done.items() if k != loop.state_field}},
    ]
    for step in bodies:
        transport = Transport(always(step), clock)
        client = harness.client(transport)
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c: loop.run(c, {}))
        assert caught.value.code == "invalid_response"
        assert len(transport.requests) == 1, "a malformed response must not be polled again"


@loops
def test_a_terminal_answer_for_the_id_is_accepted_in_either_case(
    harness: Harness, clock: FakeClock, loop: Loop
) -> None:
    shouted = {**loop.done, "id": loop.done["id"].upper()}
    client = harness.client(Transport(lambda n, now: {"body": shouted}, clock))
    assert harness.run(client, lambda c: loop.run(c, {}))["id"] == loop.done["id"].upper()


@loops
def test_an_empty_wait_id_is_refused(harness: Harness, clock: FakeClock, loop: Loop) -> None:
    transport = Transport(lambda n, now: {"body": loop.done}, clock)
    client = harness.client(transport)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.approvals.wait_for_decision(" "))
    assert caught.value.code == "invalid_request"
    assert transport.requests == []


def test_a_decided_approval_needs_a_decision_and_a_pending_one_no_decided_at(
    harness: Harness, clock: FakeClock
) -> None:
    decided = LOOPS[0].done
    pending = _approval_pending(None)
    for body in (
        {**decided, "decision": None},
        {**decided, "decision": ""},
        {**pending, "decided_at": "2026-10-27T10:00:00+00:00"},
        {k: v for k, v in decided.items() if k != "decided_at"},
    ):
        transport = Transport(always({"body": body}), clock)
        client = harness.client(transport)
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c: c.approvals.wait_for_decision("appr-1"))
        assert caught.value.code == "invalid_response"


def test_every_terminal_approval_status_ends_the_wait(harness: Harness, clock: FakeClock) -> None:
    for status in ("decided", "expired", "cancelled"):
        approval = {
            "id": "offline",
            "status": status,
            "decision": "deny" if status == "decided" else None,
            "decided_at": None,
        }
        client = harness.client(Transport(always({"body": approval}), clock))
        assert harness.run(client, lambda c: c.approvals.wait_for_decision("offline")) == approval


def test_handoff_wait_passes_its_timeout_and_spaces_polls_even_with_timeout_zero(
    harness: Harness, clock: FakeClock
) -> None:
    pending = {"id": "h-1", "kind": "ack", "state": "open"}
    transport = Transport(
        lambda n, now: {"body": pending} if n < 3 else {"body": LOOPS[2].done}, clock
    )
    client = harness.client(transport)
    harness.run(client, lambda c: c.handoffs.wait_for_result("h-1", timeout=0))
    assert all(r.url.params["timeout"] == "0" for r in transport.requests)
    assert all(r.extensions["timeout"]["read"] == 10 for r in transport.requests)
    assert [round(t, 3) for t in transport.times] == [0, 2.1, 4.2]


def test_a_handoff_negative_answer_and_expiry_are_normal_results(
    harness: Harness, clock: FakeClock
) -> None:
    for body in (
        {
            "id": "h4",
            "kind": "question",
            "state": "answered",
            "answer": {"value": "hold", "label": "Hold"},
        },
        {"id": "h4", "kind": "ack", "state": "expired"},
        {"id": "h4", "kind": "question", "state": "cancelled"},
    ):
        client = harness.client(Transport(always({"body": body}), clock))
        assert harness.run(client, lambda c: c.handoffs.wait_for_result("h4", timeout=5)) == body


def test_the_answer_arrives_after_a_pending_question_with_default_hold_timeout(
    harness: Harness, clock: FakeClock
) -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        assert request.url.path == "/api/agent/questions/q1/wait"
        body: Dict[str, Any] = (
            {"id": "q1", "kind": "question", "state": "pending", "answer": None}
            if state["n"] < 2
            else {
                "id": "q1",
                "kind": "question",
                "state": "answered",
                "answer": {"value": "ship", "label": "Ship"},
            }
        )
        return reply(body)

    rec = Recorder(fallback=handler)
    client = harness.client(rec)
    resolved = harness.run(client, lambda c: c.questions.wait_for_answer("q1"))
    assert resolved["state"] == "answered"
    assert resolved["answer"]["value"] == "ship"
    assert state["n"] == 2
    # The wait route is asked without a hold time: the server's default applies.
    assert rec.requests[0].url.query == b""


# --- retry classification and backoff ---------------------------------------


def test_transient_failure_classification_matches_the_pairing_and_cli_retry_set() -> None:
    assert is_transient_error(PingRoomError("x", code="network_error"))
    assert is_transient_error(PingRoomTimeoutError())
    assert is_transient_error(PingRoomError("x", status=429, code="public_room_rate_limited"))
    assert is_transient_error(PingRoomError("x", status=502))
    for status in (400, 401, 403, 404, 409, 422):
        assert not is_transient_error(PingRoomError("x", status=status)), status
    assert not is_transient_error(PingRoomError("x", code="invalid_response"))
    assert not is_transient_error(PingRoomError("x", code="redirect_refused", status=307))
    assert not is_transient_error(RuntimeError("plain"))


def test_transient_backoff_doubles_to_a_30_second_cap_and_yields_to_retry_after() -> None:
    error = PingRoomError("x", status=503)
    assert [transient_delay(error, n) for n in (1, 2, 3, 4, 5, 6, 7, 50)] == [
        1,
        2,
        4,
        8,
        16,
        30,
        30,
        30,
    ]
    assert [transient_delay(error, n, 0.4) for n in (1, 2, 3)] == pytest.approx([0.4, 0.8, 1.6])
    throttled = PingRoomError("x", status=429, retry_after=42)
    assert transient_delay(throttled, 1) == 42
    assert transient_delay(throttled, 9) == 42
    # Retry-After: 0 must not turn into a tight loop.
    assert transient_delay(PingRoomError("x", status=429, retry_after=0), 1) == 1


def test_every_backoff_base_is_floored_at_250_ms() -> None:
    error = PingRoomError("x", status=503)
    doubling = [0.25, 0.5, 1, 2, 4, 8, 16, 30, 30, 30]
    for base in (0, 0.001, 0.01, 0.249, 0.25):
        assert [
            transient_delay(error, n, base) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 100)
        ] == doubling
        assert transient_delay(PingRoomError("x", status=429, retry_after=0), 1, base) == 0.25
    assert transient_delay(PingRoomError("x", status=429, retry_after=3), 1, 0) == 3
    # A base above the floor is kept and doubles; a base past the cap stays put.
    assert [transient_delay(error, n, 0.3) for n in range(1, 8)] == pytest.approx(
        [0.3, 0.6, 1.2, 2.4, 4.8, 9.6, 19.2]
    )
    assert [transient_delay(error, n, 60) for n in (1, 2, 3)] == [60, 60, 60]
    # A nonsense base falls back to the default rather than to the floor.
    for base in (-5, float("nan"), float("inf")):
        assert transient_delay(error, 1, base) == 1


def test_a_cancel_set_before_a_single_poll_stops_it_before_the_request_goes_out(
    harness: Harness,
) -> None:
    rec = Recorder(fallback=static({"notifications": [], "cursor": "c"}))
    client = harness.client(rec)
    cancel = harness.new_cancel()
    cancel.set()
    for call in (
        lambda c: c.notifications.wait(after="c0", cancel=cancel),
        lambda c: c.notifications.wait_for_acknowledgement("n1", cancel=cancel),
        lambda c: c.approvals.wait("a1", cancel=cancel),
        lambda c: c.questions.wait("q1", cancel=cancel),
        lambda c: c.attachments.content("att1", cancel=cancel),
    ):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, call)
        assert caught.value.code == "cancelled"
    assert rec.requests == []
