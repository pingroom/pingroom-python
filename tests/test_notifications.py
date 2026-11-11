"""``notifications.*``: list, get, wait, wait_for_acknowledgement, replace, withdraw and listen."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

import httpx
import pytest

from conftest import FakeClock, Harness, Recorder, raw_path, reply, static
from pingroom import (
    COMMON_ERROR_CODES,
    REPLACE_ERROR_CODES,
    UNSET,
    WITHDRAW_ERROR_CODES,
    PingRoomError,
)


def ping(identifier: str) -> Any:
    return {
        "id": identifier,
        "message": identifier,
        "created_at": "now",
        "action_number": None,
        "action_icon": None,
        "trigger_source": None,
    }


def invalid(error: PingRoomError) -> bool:
    return error.code == "invalid_request" and error.status == 0


# --- list ------------------------------------------------------------------


def test_list_forwards_limit_page_room_and_type_and_adds_room_code(harness: Harness) -> None:
    room = {
        "id": "r1",
        "invite_code": "AB12",
        "name": "Deploys",
        "icon": "bell",
        "color": "#ff0000",
        "is_public": False,
    }
    payload: Dict[str, Any] = {
        "data": [{"id": "n1", "message": "Ready", "room": room, "action_state": None}],
        "current_page": 2,
        "per_page": 10,
        "total": 23,
        "last_page": 3,
        "has_more": True,
        "next_page_url": "/api/agent/notifications?page=3",
    }
    rec = Recorder(fallback=static(payload))
    client = harness.client(rec)
    result = harness.run(
        client,
        lambda c: c.notifications.list(limit=10, page=2, room_id="r1", type="received"),
    )
    assert rec.last.method == "GET"
    assert rec.last.url.path == "/api/agent/notifications"
    assert rec.query() == {"limit": "10", "page": "2", "room_id": "r1", "type": "received"}
    assert result == {
        **payload,
        "data": [{**payload["data"][0], "room": {**room, "code": "AB12"}}],
    }


def test_list_maps_the_deprecated_page_size_and_gives_limit_precedence(harness: Harness) -> None:
    rec = Recorder(fallback=static({"data": []}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.list(notifications_per_page=10))
    harness.run(client, lambda c: c.notifications.list(notifications_per_page=10, limit=25, page=3))
    harness.run(client, lambda c: c.notifications.list())
    assert rec.requests[0].url.query == b"limit=10"
    assert rec.query(1) == {"limit": "25", "page": "3"}
    assert rec.requests[2].url.query == b""
    assert all("notifications_per_page" not in r.url.params for r in rec.requests)


def test_list_sends_tag_and_date_and_leaves_the_query_alone_without_them(harness: Harness) -> None:
    rec = Recorder(fallback=static({"data": []}))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.notifications.list(
            tag="deploy", room_id="0197c3a0-0000-7000-8000-000000000001", limit=5, date="2026-11-11"
        ),
    )
    assert rec.query() == {
        "tag": "deploy",
        "room_id": "0197c3a0-0000-7000-8000-000000000001",
        "limit": "5",
        "date": "2026-11-11",
    }
    # The server lowercases the filter, so a padded, mixed-case one is accepted.
    harness.run(client, lambda c: c.notifications.list(tag=" Deploy "))
    assert rec.query()["tag"] == " Deploy "
    harness.run(client, lambda c: c.notifications.list(type="received"))
    assert "tag" not in rec.query()


@pytest.mark.parametrize("tag", ["bad tag", "", 5])
def test_list_refuses_a_tag_that_cannot_exist(harness: Harness, tag: Any) -> None:
    rec = Recorder(fallback=static({"data": []}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.list(tag=tag))
    assert invalid(caught.value)
    assert rec.requests == []


def test_list_tolerates_a_page_without_rows(harness: Harness) -> None:
    client = harness.client(static({"data": [{"id": "n1"}], "total": 1}))
    result = harness.run(client, lambda c: c.notifications.list())
    assert result["data"] == [{"id": "n1"}]  # a row without a room gains none
    client = harness.client(static({"unexpected": True}))
    assert harness.run(client, lambda c: c.notifications.list()) == {"unexpected": True}


# --- get -------------------------------------------------------------------


def test_get_fetches_one_notification_and_url_encodes_its_id(harness: Harness) -> None:
    body = {
        "id": "n/1 ?",
        "message": "Please acknowledge",
        "action_state": {"status": "open", "requires_ack": True, "acked_by": None},
    }
    rec = Recorder(fallback=static(body))
    client = harness.client(rec)
    notification = harness.run(client, lambda c: c.notifications.get("n/1 ?"))
    assert notification["action_state"]["status"] == "open"
    assert raw_path(rec.last) == b"/api/agent/notifications/n%2F1%20%3F"
    assert rec.last.method == "GET"


def test_get_reads_edited_and_withdrawn_at_through(harness: Harness) -> None:
    body = {**ping("n1"), "edited_at": "2026-11-11T12:00:00.000000Z", "withdrawn_at": None}
    client = harness.client(static(body))
    detail = harness.run(client, lambda c: c.notifications.get("n1"))
    assert detail["edited_at"] == "2026-11-11T12:00:00.000000Z"
    assert detail["withdrawn_at"] is None


@pytest.mark.parametrize("identifier", ["", " ", None])
def test_get_refuses_an_empty_id(harness: Harness, identifier: Any) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.get(identifier))
    assert invalid(caught.value)
    assert rec.requests == []


# --- wait and acknowledgement ----------------------------------------------


def test_wait_sends_cursor_timeout_limit_and_tag(harness: Harness) -> None:
    rec = Recorder(fallback=static({"notifications": [ping("n2")], "cursor": "cur2"}))
    client = harness.client(rec)
    result = harness.run(
        client, lambda c: c.notifications.wait(after="cur1", timeout=5, limit=10, tag="billing")
    )
    assert result["cursor"] == "cur2"
    assert rec.last.url.path == "/api/agent/notifications/wait"
    assert rec.query() == {"after": "cur1", "timeout": "5", "limit": "10", "tag": "billing"}
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.wait(tag="bad tag"))
    assert invalid(caught.value)


def test_wait_without_arguments_sends_no_query(harness: Harness) -> None:
    rec = Recorder(fallback=static({"notifications": [], "cursor": "c"}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.wait())
    assert rec.last.url.query == b""


def test_wait_for_acknowledgement_sends_timeout_and_returns_the_terminal_state(
    harness: Harness,
) -> None:
    body = {
        "id": "n/1",
        "action_state": {
            "status": "acked",
            "requires_ack": True,
            "acked_by": {"id": "u1", "name": "Sam", "profile_image": None},
            "acked_at": "now",
            "expires_at": None,
        },
    }
    rec = Recorder(fallback=static(body))
    client = harness.client(rec)
    result = harness.run(
        client, lambda c: c.notifications.wait_for_acknowledgement("n/1", timeout=7)
    )
    assert result["action_state"]["status"] == "acked"
    assert result["action_state"]["acked_by"]["name"] == "Sam"
    assert raw_path(rec.last) == b"/api/agent/notifications/n%2F1/ack/wait"
    assert rec.query() == {"timeout": "7"}


def test_confirmation_progress_survives_every_read(harness: Harness) -> None:
    state = {
        "status": "open",
        "mode": "all",
        "confirmed_count": 1,
        "required_count": 2,
        "confirmed_user_ids": ["member-1"],
        "can_ack": False,
    }
    client = harness.client(static({"id": "ping-1", "action_state": state}))
    assert harness.run(client, lambda c: c.notifications.get("ping-1"))["action_state"] == state
    result = harness.run(
        client, lambda c: c.notifications.wait_for_acknowledgement("ping-1", timeout=0)
    )
    assert result["action_state"] == state


# --- replace ---------------------------------------------------------------

REPLACE_PATH = "/api/agent/rooms/ab12/notifications/n1"
REPLACED = {**ping("n1"), "message": "Build 41: green", "edited_at": "2026-11-11T12:00:00.000000Z"}


def test_replace_puts_the_new_text_and_returns_the_ping_with_edited_at(harness: Harness) -> None:
    rec = Recorder(routes={f"PUT {REPLACE_PATH}": static(REPLACED)})
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "Build 41: green"))
    assert result["edited_at"] == "2026-11-11T12:00:00.000000Z"
    assert rec.last.method == "PUT"
    assert rec.last.url.path == REPLACE_PATH
    # title and data were left out, so they are not on the wire: the server keeps them.
    assert rec.body() == {"message": "Build 41: green"}
    assert "idempotency-key" not in rec.last.headers


def test_replace_sends_title_and_data_as_given_and_none_clears_them(harness: Harness) -> None:
    rec = Recorder(fallback=static(REPLACED))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.notifications.replace("ab12", "n1", "m", title="Deploy", data={"build": 41}),
    )
    harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "m", title=None, data=None))
    harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "m", title=""))
    harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "m", title=UNSET))
    assert rec.body(0) == {"message": "m", "title": "Deploy", "data": {"build": 41}}
    assert rec.body(1) == {"message": "m", "title": None, "data": None}
    assert rec.body(2) == {"message": "m", "title": ""}
    assert rec.body(3) == {"message": "m"}


def test_replace_url_encodes_both_segments_and_refuses_empty_ones(harness: Harness) -> None:
    rec = Recorder(fallback=static(REPLACED))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.replace("ab 12", "n/1", "m"))
    assert raw_path(rec.last) == b"/api/agent/rooms/ab%2012/notifications/n%2F1"
    for args in (("", "n1", "m"), ("ab12", " ", "m"), ("ab12", "n1", "")):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, a=args: c.notifications.replace(*a))
        assert invalid(caught.value)
    assert len(rec.requests) == 1


def test_replace_checks_length_and_data_like_a_send(harness: Harness) -> None:
    rec = Recorder(fallback=static(REPLACED))
    client = harness.client(rec)
    cases = [
        (lambda c: c.notifications.replace("ab12", "n1", "m" * 161), "invalid_request"),
        (lambda c: c.notifications.replace("ab12", "n1", "m", title="t" * 41), "invalid_request"),
        (lambda c: c.notifications.replace("ab12", "n1", "m", data=[1]), "invalid_data"),
    ]
    for call, code in cases:
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, call)
        assert caught.value.code == code
    assert rec.requests == []


def test_replace_refuses_buttons_before_any_request(harness: Harness) -> None:
    rec = Recorder(fallback=static(REPLACED))
    client = harness.client(rec)
    actions = {"actions": [{"label": "Open", "type": "open_url", "url": "https://example.com/"}]}
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "x", data=actions))
    assert caught.value.code == "invalid_data"
    assert caught.value.message.startswith("A replacement cannot carry buttons")
    assert rec.requests == []


REPLACE_REFUSALS = [
    (404, "notification_not_found"),
    (403, "not_ping_owner"),
    (422, "unsupported_room"),
    (422, "unsupported_question"),
    (422, "unsupported_handoff"),
    (422, "unsupported_live_status"),
    (422, "unsupported_claim"),
    (422, "unsupported_ack"),
    (422, "unsupported_checklist"),
    (422, "unsupported_source"),
    (422, "unsupported_actions"),
    (409, "replace_window_closed"),
    (409, "replace_limit_reached"),
    (409, "ping_withdrawn"),
]


def test_the_replace_refusals_are_exactly_the_exported_codes() -> None:
    assert [code for _, code in REPLACE_REFUSALS] == list(REPLACE_ERROR_CODES)
    assert len(set(REPLACE_ERROR_CODES)) == len(REPLACE_ERROR_CODES)
    for code in REPLACE_ERROR_CODES:
        assert code not in COMMON_ERROR_CODES


@pytest.mark.parametrize("status,code", REPLACE_REFUSALS)
def test_every_replace_refusal_surfaces_its_status_and_code(
    harness: Harness, status: int, code: str
) -> None:
    client = harness.client(static({"code": code, "message": f"refused: {code}"}, status))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "m"))
    assert (caught.value.status, caught.value.code) == (status, code)


def test_a_replace_404_without_a_code_is_a_plain_404(harness: Harness) -> None:
    client = harness.client(static({"message": "The route could not be found."}, 404))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.replace("ab12", "n1", "m"))
    assert caught.value.status == 404
    assert caught.value.code is None


# --- withdraw --------------------------------------------------------------

WITHDRAW_PATH = "/api/agent/rooms/ab12/notifications/n1/withdraw"
WITHDRAWN = {**ping("n1"), "withdrawn_at": "2026-11-11T12:00:00.000000Z"}


def test_withdraw_posts_to_the_withdraw_route_with_an_empty_object(harness: Harness) -> None:
    rec = Recorder(routes={f"POST {WITHDRAW_PATH}": static(WITHDRAWN)})
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.notifications.withdraw("ab12", "n1"))
    assert result["withdrawn_at"] == "2026-11-11T12:00:00.000000Z"
    assert rec.last.method == "POST"
    assert rec.last.url.path == WITHDRAW_PATH
    assert rec.last.content == b"{}"
    assert rec.last.headers["content-type"] == "application/json"
    assert "idempotency-key" not in rec.last.headers


def test_withdraw_url_encodes_both_segments_and_refuses_empty_ones(harness: Harness) -> None:
    rec = Recorder(fallback=static(WITHDRAWN))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.withdraw("ab 12", "n/1"))
    assert raw_path(rec.last) == b"/api/agent/rooms/ab%2012/notifications/n%2F1/withdraw"
    for args in (("", "n1"), ("ab12", " ")):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, a=args: c.notifications.withdraw(*a))
        assert invalid(caught.value)
    assert len(rec.requests) == 1


WITHDRAW_REFUSALS = [
    (404, "notification_not_found"),
    (403, "not_ping_owner"),
    (422, "unsupported_room"),
    (422, "unsupported_question"),
    (422, "unsupported_handoff"),
    (422, "unsupported_live_status"),
    (422, "unsupported_claim"),
    (422, "unsupported_ack"),
    (422, "unsupported_checklist"),
    (422, "unsupported_source"),
    (409, "withdraw_window_closed"),
]


def test_the_withdraw_refusals_are_exactly_the_exported_codes() -> None:
    assert [code for _, code in WITHDRAW_REFUSALS] == list(WITHDRAW_ERROR_CODES)


@pytest.mark.parametrize("status,code", WITHDRAW_REFUSALS)
def test_every_withdraw_refusal_surfaces_its_status_and_code(
    harness: Harness, status: int, code: str
) -> None:
    client = harness.client(static({"code": code, "message": code}, status))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.notifications.withdraw("ab12", "n1"))
    assert (caught.value.status, caught.value.code) == (status, code)


# --- listen ----------------------------------------------------------------


class Script:
    """Answers each request from a list of steps; the last step repeats."""

    def __init__(self, steps: List[Any], clock: FakeClock) -> None:
        self.steps = steps
        self.clock = clock
        self.requests: List[httpx.Request] = []
        self.times: List[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock.now())
        step = self.steps[min(len(self.requests) - 1, len(self.steps) - 1)]
        if "throws" in step:
            raise step["throws"](request)
        return reply(step.get("body", {}), step.get("status", 200), step.get("headers"))

    def gaps(self) -> List[float]:
        return [round(b - a, 3) for a, b in zip(self.times, self.times[1:])]


def listening(cancel: Any, **options: Any) -> Callable[[Any], Any]:
    """``listen()`` from cursor ``c0`` with the given cancel event and options."""
    return lambda c: c.notifications.listen(after="c0", cancel=cancel, **options)


def network_failure(request: httpx.Request) -> Exception:
    return httpx.ConnectError("fetch failed", request=request)


def test_listen_reads_the_head_first_then_polls_forward_and_stops_on_cancel(
    harness: Harness, clock: FakeClock
) -> None:
    n = {"count": 0}
    cancel = harness.new_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agent/notifications/wait"
        n["count"] += 1
        if n["count"] == 1:
            return reply({"notifications": [], "cursor": "c0"})
        identifier, cursor = ("a", "c1") if n["count"] == 2 else ("b", "c2")
        return reply({"notifications": [ping(identifier)], "cursor": cursor})

    rec = Recorder(fallback=handler)
    client = harness.client(rec)
    got = harness.collect(
        client,
        lambda c: c.notifications.listen(cancel=cancel),
        on_item=lambda item: cancel.set() if item["id"] == "b" else None,
    )
    assert [p["id"] for p in got] == ["a", "b"]
    # The head read: an immediate answer, no cursor. Then polls ahead of the cursor.
    assert rec.query(0) == {"timeout": "0"}
    assert rec.query(1) == {"after": "c0"}
    assert rec.query(2) == {"after": "c1"}


def test_listen_passes_timeout_limit_and_tag_on_forward_polls_only(
    harness: Harness, clock: FakeClock
) -> None:
    cancel = harness.new_cancel()
    rec = Recorder(fallback=static({"notifications": [], "cursor": "head"}))
    client = harness.client(rec)
    clock.at(5, cancel.set)
    harness.collect(
        client,
        lambda c: c.notifications.listen(tag="billing", timeout=3, limit=7, cancel=cancel),
    )
    assert rec.query(0) == {"timeout": "0"}
    assert rec.query(1) == {"after": "head", "timeout": "3", "limit": "7", "tag": "billing"}


def test_listen_refuses_a_bad_tag_on_the_first_step(harness: Harness) -> None:
    rec = Recorder(fallback=static({"notifications": [], "cursor": "c"}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.collect(client, lambda c: c.notifications.listen(tag="bad tag"))
    assert invalid(caught.value)
    assert rec.requests == []


def test_listen_ends_by_raising_a_401_even_with_on_error(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script([{"status": 401, "body": {"message": "Unauthenticated."}}], clock)
    client = harness.client(script)
    handled: List[Any] = []
    with pytest.raises(PingRoomError) as caught:
        harness.collect(
            client,
            lambda c: c.notifications.listen(after="c0", backoff=0.001, on_error=handled.append),
        )
    assert caught.value.status == 401
    assert len(script.requests) == 1
    assert handled == []


def test_listen_retries_only_transient_failures_and_keeps_delivering(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script(
        [
            {"status": 503, "body": {}},
            {"throws": network_failure},
            {"body": {"notifications": [ping("a")], "cursor": "c1"}},
        ],
        clock,
    )
    client = harness.client(script)
    handled: List[PingRoomError] = []
    got = harness.collect(
        client,
        lambda c: c.notifications.listen(after="c0", backoff=0.4, on_error=handled.append),
        stop_after=1,
    )
    assert [p["id"] for p in got] == ["a"]
    assert [(e.status, e.code) for e in handled] == [(503, None), (0, "network_error")]
    # The 0.4 s and 0.8 s backoff steps must also respect the 2.1 s poll interval.
    assert script.gaps() == [2.1, 2.1]


def test_listen_waits_out_a_retry_after_longer_than_the_poll_interval(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script(
        [
            {"status": 429, "body": {"code": "rate_limited", "message": "x", "retry_after": 3}},
            {"body": {"notifications": [ping("b")], "cursor": "c2"}},
        ],
        clock,
    )
    client = harness.client(script)
    got = harness.collect(
        client, lambda c: c.notifications.listen(after="c0", backoff=0.001), stop_after=1
    )
    assert [p["id"] for p in got] == ["b"]
    assert script.gaps() == [3.0]


def test_listen_retries_a_transient_failure_of_its_first_head_read(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script(
        [
            {"status": 502, "body": {}},
            {"body": {"notifications": [], "cursor": "head"}},
            {"body": {"notifications": [ping("c")], "cursor": "c3"}},
        ],
        clock,
    )
    client = harness.client(script)
    got = harness.collect(client, lambda c: c.notifications.listen(), stop_after=1)
    assert [p["id"] for p in got] == ["c"]
    assert script.requests[1].url.params["timeout"] == "0"
    assert "after" not in script.requests[1].url.params
    assert script.requests[2].url.params["after"] == "head"


def test_listen_combines_the_poll_interval_with_a_growing_backoff(
    harness: Harness, clock: FakeClock
) -> None:
    # A base of 0 never grew, and 1 ms reached only 32 ms: about 31 requests a
    # second against a route limited to 30 a minute. Every base below the floor
    # now behaves alike.
    for backoff in (0, 0.001, 0.04, 0.25):
        script = Script([{"status": 503, "body": {}}], clock)
        client = harness.client(script)
        cancel = harness.new_cancel()
        clock.at(clock.elapsed() + 100, cancel.set)
        harness.collect(client, listening(cancel, backoff=backoff))
        assert script.gaps() == [2.1, 2.1, 2.1, 2.1, 4.0, 8.0, 16.0, 30.0, 30.0], backoff


def test_listen_keeps_a_large_backoff_and_caps_at_30_seconds(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script([{"status": 503, "body": {}}], clock)
    client = harness.client(script)
    cancel = harness.new_cancel()
    clock.at(100, cancel.set)
    harness.collect(client, lambda c: c.notifications.listen(after="c0", cancel=cancel, backoff=5))
    assert script.gaps() == [5.0, 10.0, 20.0, 30.0, 30.0]


def test_cancelling_listen_during_the_retry_interval_ends_it_quietly(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script([{"status": 503, "body": {}}], clock)
    client = harness.client(script)
    cancel = harness.new_cancel()
    clock.at(1.5, cancel.set)
    got = harness.collect(
        client, lambda c: c.notifications.listen(after="c0", cancel=cancel, backoff=0)
    )
    assert got == []
    assert clock.elapsed() == 1.5
    assert len(script.requests) == 1


def test_listen_stays_below_30_polls_a_minute_across_failures_and_empty_answers(
    harness: Harness, clock: FakeClock
) -> None:
    steps = [
        {"status": 503, "body": {}}
        if i % 2 == 0
        else {"body": {"notifications": [], "cursor": "c0"}}
        for i in range(60)
    ]
    script = Script(steps, clock)
    client = harness.client(script)
    cancel = harness.new_cancel()
    clock.at(60, cancel.set)
    harness.collect(client, lambda c: c.notifications.listen(after="c0", timeout=0, cancel=cancel))
    assert clock.elapsed() == 60
    assert len(script.requests) == 29
    assert all(gap == 2.1 for gap in script.gaps())


def test_listen_does_not_spin_when_the_server_answers_at_once(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script([{"body": {"notifications": [], "cursor": "c0"}}], clock)
    client = harness.client(script)
    cancel = harness.new_cancel()
    clock.at(2.0, cancel.set)
    harness.collect(client, lambda c: c.notifications.listen(after="c0", timeout=0, cancel=cancel))
    # The route allows 30 a minute per human, shared with their other listeners.
    assert len(script.requests) == 1


def test_listen_starts_empty_polls_2_1_s_apart_and_repolls_a_non_empty_batch_at_once(
    harness: Harness, clock: FakeClock
) -> None:
    script = Script(
        [
            {"body": {"notifications": [ping("a")], "cursor": "c1"}},
            {"body": {"notifications": [], "cursor": "c1"}},
            {"body": {"notifications": [], "cursor": "c1"}},
        ],
        clock,
    )
    client = harness.client(script)
    cancel = harness.new_cancel()
    clock.at(5, cancel.set)
    got = harness.collect(
        client, lambda c: c.notifications.listen(after="c0", timeout=0, cancel=cancel)
    )
    assert [p["id"] for p in got] == ["a"]
    assert script.gaps()[:2] == [0.0, 2.1]


@pytest.mark.parametrize(
    "body",
    [
        "<html>Please sign in to the hotspot</html>",
        {},
        {"notifications": "none", "cursor": "c1"},
        {"notifications": [None], "cursor": "c1"},
        {"notifications": [], "cursor": 7},
        {"notifications": [], "cursor": None},
    ],
)
def test_listen_turns_a_2xx_that_is_not_a_wait_answer_into_invalid_response(
    harness: Harness, clock: FakeClock, body: Any
) -> None:
    text = body if isinstance(body, str) else None
    script = Script([{"body": body}], clock) if text is None else None
    handler = script if script is not None else (lambda request: reply(text=text))
    client = harness.client(handler)
    with pytest.raises(PingRoomError) as caught:
        harness.collect(client, lambda c: c.notifications.listen(after="c0"))
    assert caught.value.code == "invalid_response"
    assert caught.value.status == 0
    if script is not None:
        assert len(script.requests) == 1


def test_the_initial_head_read_must_carry_a_cursor(harness: Harness, clock: FakeClock) -> None:
    client = harness.client(static({"notifications": []}))
    with pytest.raises(PingRoomError) as caught:
        harness.collect(client, lambda c: c.notifications.listen())
    assert caught.value.code == "invalid_response"


def test_a_closed_iterator_stops_polling(harness: Harness, clock: FakeClock) -> None:
    rec = Recorder(fallback=static({"notifications": [ping("a"), ping("b")], "cursor": "c1"}))
    client = harness.client(rec)
    got = harness.collect(client, lambda c: c.notifications.listen(after="c0"), stop_after=1)
    assert [p["id"] for p in got] == ["a"]
    assert len(rec.requests) == 1
