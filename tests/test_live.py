"""``live.push/get/claim`` and the ``live_status`` builders."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from conftest import Harness, Recorder, raw_path, reply, static
from pingroom import LIVE_CLAIM_ERROR_CODES, PingRoomError, live_status
from pingroom.live_status import LIVE_STATUS_LIMITS, LIVE_TEMPLATES

NOTIFICATION_ID = "0198f3c2-6f0e-7d3a-9a55-3b1c2d4e5f60"
STARTED = {"notification_id": "n1", "correlation_id": "c", "state": "started", "idempotent": False}


# --- builders --------------------------------------------------------------


def test_builders_stamp_their_template_and_pass_content_through() -> None:
    assert live_status.progress(
        "deploy-1", {"state": "running", "progress": 0.4, "message": "Building"}
    ) == {
        "correlation_id": "deploy-1",
        "live_status": {
            "template": "progress",
            "state": "running",
            "progress": 0.4,
            "message": "Building",
        },
    }


def test_steps_carries_its_labels() -> None:
    ping = live_status.steps(
        "rel-1", {"state": "running", "steps": ["Build", "Ship"], "current_step": 0}
    )
    assert ping["live_status"]["template"] == "steps"
    assert ping["live_status"]["steps"] == ["Build", "Ship"]


def test_alert_sets_the_category_not_a_template() -> None:
    ping = live_status.alert("incident-1", {"state": "running", "message": "5xx climbing"})
    assert ping["live_status"]["category"] == "alert"
    assert "template" not in ping["live_status"]


def test_terminal_builders_stay_sparse() -> None:
    # The server merges a terminal leg over the stored stream, so a bare
    # {state: done} keeps the scoreboard on the final frame.
    assert live_status.done("deploy-1", "Live") == {
        "correlation_id": "deploy-1",
        "live_status": {"state": "done", "message": "Live"},
    }
    assert live_status.failed("deploy-1")["live_status"] == {"state": "failed"}
    assert live_status.done("c", "")["live_status"] == {"state": "done"}


def test_builders_merge_the_rest_of_the_ping_body() -> None:
    ping = live_status.status(
        "c1",
        {"state": "running"},
        title="Deploy",
        action=2,
        requires_ack=True,
        data={"k": 1},
        tags=["x"],
        ack_timeout_seconds=60,
    )
    assert ping == {
        "correlation_id": "c1",
        "live_status": {"template": "status", "state": "running"},
        "title": "Deploy",
        "action": 2,
        "data": {"k": 1},
        "tags": ["x"],
        "requires_ack": True,
        "ack_timeout_seconds": 60,
    }
    assert live_status.done("c1", "ok", tags=("x",))["tags"] == ["x"]


def test_a_builder_copies_the_live_status_instead_of_editing_it() -> None:
    live: Dict[str, Any] = {"state": "running", "message": "x"}
    ping = live_status.status("c", live)  # type: ignore[arg-type]
    ping["live_status"]["message"] = "changed"
    assert live == {"state": "running", "message": "x"}
    assert ping["live_status"]["template"] == "status"


def test_every_template_has_a_builder_that_stamps_it() -> None:
    for template in LIVE_TEMPLATES:
        builder = getattr(live_status, template)
        assert builder("c", {"state": "running"})["live_status"]["template"] == template


def test_claim_stamps_the_claim_template_and_carries_the_request_in_title() -> None:
    assert live_status.claim(
        "milk-1", {"state": "running", "deadline_at": 1794148500}, title="Buy milk"
    ) == {
        "correlation_id": "milk-1",
        "title": "Buy milk",
        "live_status": {"template": "claim", "state": "running", "deadline_at": 1794148500},
    }


def test_timer_value_stats_and_segmented_progress_pass_their_fields_through() -> None:
    assert live_status.timer(
        "build-9",
        {"state": "running", "started_at": 1794146700, "message": "Compiling"},
        title="Build",
    ) == {
        "correlation_id": "build-9",
        "title": "Build",
        "live_status": {
            "template": "timer",
            "state": "running",
            "started_at": 1794146700,
            "message": "Compiling",
        },
    }
    assert "started_at" not in live_status.timer("b", {"state": "running"})["live_status"]
    assert live_status.value(
        "mrr-1",
        {
            "state": "running",
            "value": "$12.4k",
            "value_label": "MRR",
            "delta": "+3.2%",
            "trend": "up",
        },
    )["live_status"] == {
        "template": "value",
        "state": "running",
        "value": "$12.4k",
        "value_label": "MRR",
        "delta": "+3.2%",
        "trend": "up",
    }
    stats = [
        {"label": "RPS", "value": "1.2k"},
        {"label": "p99", "value": "180ms"},
        {"label": "5xx", "value": "0.1%"},
        {"label": "CPU", "value": "45%"},
    ]
    assert (
        live_status.stats("h", {"state": "running", "stats": stats})["live_status"]["stats"]
        == stats
    )
    assert live_status.progress("r", {"state": "running", "segments": 5, "current_segment": 2})[
        "live_status"
    ] == {"template": "progress", "state": "running", "segments": 5, "current_segment": 2}


def test_the_builders_do_not_enforce_the_server_limits() -> None:
    value = "x" * 25
    assert (
        live_status.value("v", {"state": "running", "value": value})["live_status"]["value"]
        == value
    )
    assert live_status.stats("s", {"state": "running", "stats": []})["live_status"]["stats"] == []


def test_the_limits_are_exported() -> None:
    assert dict(LIVE_STATUS_LIMITS) == {
        "value_length": 24,
        "value_label_length": 24,
        "delta_length": 12,
        "stats_min": 4,
        "stats_max": 8,
        "stat_label_length": 24,
        "stat_value_length": 24,
        "segments_min": 2,
        "segments_max": 20,
    }


# --- push ------------------------------------------------------------------


def test_push_posts_the_body_to_the_room_live_route(harness: Harness) -> None:
    rec = Recorder(fallback=static(STARTED, 201))
    client = harness.client(rec)
    ping = live_status.timer("c", {"state": "running", "started_at": 1794146700}, tags=["nightly"])
    result = harness.run(client, lambda c: c.live.push("AB12CD", ping))
    assert result["state"] == "started"
    assert rec.last.method == "POST"
    assert rec.last.url.path == "/api/agent/rooms/AB12CD/live"
    assert rec.body() == {
        "correlation_id": "c",
        "live_status": {"template": "timer", "state": "running", "started_at": 1794146700},
        "tags": ["nightly"],
    }


def test_push_validates_tags_data_and_correlation_id_before_sending(harness: Harness) -> None:
    rec = Recorder(fallback=static(STARTED, 201))
    client = harness.client(rec)
    bad: List[Any] = [
        (live_status.done("c1", tags=["bad tag"]), "invalid_request"),
        ({"correlation_id": "", "live_status": {"state": "done"}}, "invalid_request"),
        (live_status.status("c1", {"state": "running"}, data={"big": "x" * 9000}), "invalid_data"),
        (
            live_status.status("c1", {"state": "running"}, data={"actions": [{"label": "x"}]}),
            "invalid_data",
        ),
    ]
    for ping, code in bad:
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, p=ping: c.live.push("ab12", p))
        assert caught.value.code == code
    assert caught.value.message.startswith("A live-progress stream cannot carry buttons")
    assert rec.requests == []
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.live.push(" ", live_status.done("c")))


def test_push_surfaces_a_claim_start_refusal(harness: Harness) -> None:
    client = harness.client(static({"code": "claim_private_rooms_only", "message": "no"}, 422))
    with pytest.raises(PingRoomError) as caught:
        harness.run(
            client,
            lambda c: c.live.push("ab12", live_status.claim("c", {"state": "running"}, title="x")),
        )
    assert caught.value.code == "claim_private_rooms_only"


# --- get -------------------------------------------------------------------


def test_get_reads_back_a_stream_and_encodes_the_correlation_id(harness: Harness) -> None:
    snapshot = {
        "notification_id": "n1",
        "correlation_id": "a/b c",
        "state": "running",
        "progress": 0.5,
    }
    rec = Recorder(fallback=static(snapshot))
    client = harness.client(rec)
    assert harness.run(client, lambda c: c.live.get("AB12CD", "a/b c")) == snapshot
    assert rec.last.method == "GET"
    assert raw_path(rec.last) == b"/api/agent/rooms/AB12CD/live/a%2Fb%20c"


def test_a_room_scoped_403_propagates_instead_of_reading_as_no_stream(harness: Harness) -> None:
    client = harness.client(static({"code": "room_not_granted", "message": "not granted"}, 403))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.get("AB12CD", "corr-1"))
    assert (caught.value.code, caught.value.status) == ("room_not_granted", 403)


def test_get_resolves_none_for_a_genuinely_absent_stream(harness: Harness) -> None:
    client = harness.client(
        static(
            {
                "code": "stream_not_found",
                "message": "No live stream for that correlation id in the last 24 hours.",
            },
            404,
        )
    )
    assert harness.run(client, lambda c: c.live.get("AB12CD", "corr-1")) is None


def test_a_404_that_is_not_stream_not_found_throws(harness: Harness) -> None:
    # A mistyped or deleted room code is the route-binding 404, which carries no
    # stream code. Reading it as "no stream" would make a restarted producer
    # open a new stream instead of failing.
    client = harness.client(static({"message": "Resource not found."}, 404))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.get("NOPE99", "corr-1"))
    assert (caught.value.status, caught.value.code) == (404, None)


# --- claim -----------------------------------------------------------------


def receipt(**overrides: Any) -> Dict[str, Any]:
    return {
        "notification_id": NOTIFICATION_ID,
        "correlation_id": "milk-1",
        "state": "running",
        "idempotent": False,
        "claim": {
            "status": "claimed",
            "by": {"id": "member-1", "name": "Alex", "image_url": None},
            "at": 1794146700,
            "late": False,
        },
        **overrides,
    }


def test_claim_posts_the_action_to_the_transition_route(harness: Harness) -> None:
    rec = Recorder(fallback=static(receipt()))
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID, "claim"))
    assert rec.last.method == "POST"
    assert rec.last.url.path == f"/api/agent/rooms/AB12CD/live/{NOTIFICATION_ID}/claim"
    assert rec.body() == {"action": "claim"}
    assert result["claim"]["by"]["name"] == "Alex"
    assert result["idempotent"] is False


@pytest.mark.parametrize(
    "action,status,state",
    [
        ("claim", "claimed", "running"),
        ("release", "open", "running"),
        ("done", "done", "done"),
        ("cancel", "cancelled", "failed"),
    ],
)
@pytest.mark.parametrize("idempotent", [False, True])
def test_claim_accepts_each_documented_transition_without_dropping_additive_fields(
    harness: Harness, action: str, status: str, state: str, idempotent: bool
) -> None:
    body = receipt(state=state, idempotent=idempotent, extra="future-field")
    body["claim"]["status"] = status
    if action in ("release", "cancel"):
        body["claim"].update({"by": None, "at": None})
    calls = 0

    def handler(request: Any) -> Any:
        nonlocal calls
        calls += 1
        return reply(body)

    client = harness.client(handler)
    result = harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID.upper(), action))
    assert result == body
    assert calls == 1


def expired_release(**overrides: Any) -> Dict[str, Any]:
    return {
        "notification_id": NOTIFICATION_ID,
        "correlation_id": "milk-1",
        "state": "failed",
        "idempotent": False,
        "claim": {
            "status": "expired",
            "by": None,
            "at": None,
            "late": False,
            "realerted_at": 1794146000,
            "ended_by": "member-1",
        },
        **overrides,
    }


@pytest.mark.parametrize("idempotent", [False, True])
def test_release_past_the_deadline_resolves_with_the_expired_receipt_once(
    harness: Harness, idempotent: bool
) -> None:
    body = expired_release(idempotent=idempotent, extra="future-field")
    rec = Recorder(fallback=static(body))
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID, "release"))
    assert result == body
    assert result["claim"]["status"] == "expired"
    assert result["state"] == "failed"
    assert len(rec.requests) == 1
    assert rec.body() == {"action": "release"}


def test_the_expired_receipt_is_release_only_and_only_as_expired_failed(harness: Harness) -> None:
    def wrong(status: str, state: str) -> Dict[str, Any]:
        base = expired_release()
        return expired_release(state=state, claim={**base["claim"], "status": status})

    cases: List[Any] = [
        *[(action, expired_release()) for action in ("claim", "done", "cancel")],
        ("release", wrong("expired", "running")),
        ("release", wrong("expired", "done")),
        ("release", wrong("open", "failed")),
        ("release", wrong("unfinished", "failed")),
        ("release", wrong("cancelled", "failed")),
        ("release", expired_release(notification_id="another-notification")),
        ("release", expired_release(claim={**expired_release()["claim"], "late": "false"})),
        ("release", expired_release(claim={**expired_release()["claim"], "at": "1794146700"})),
    ]
    for action, body in cases:
        calls = 0

        def handler(request: Any, b: Any = body) -> Any:
            nonlocal calls
            calls += 1
            return reply(b)

        client = harness.client(handler)
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, a=action: c.live.claim("AB12CD", NOTIFICATION_ID, a))
        assert (caught.value.code, caught.value.status) == ("invalid_response", 0), (action, body)
        assert calls == 1


def _receipt_without(key: str) -> Dict[str, Any]:
    body = receipt()
    del body[key]
    return body


def _claim_with(**change: Any) -> Dict[str, Any]:
    body = receipt()
    body["claim"].update(change)
    return body


def _claim_without(key: str) -> Dict[str, Any]:
    body = receipt()
    del body["claim"][key]
    return body


INVALID_RECEIPTS: List[Any] = [
    None,
    [],
    {},
    True,
    1,
    receipt(notification_id="another-notification"),
    _receipt_without("notification_id"),
    receipt(correlation_id=""),
    receipt(idempotent="true"),
    _receipt_without("idempotent"),
    receipt(state="done"),
    receipt(claim=None),
    _claim_with(status="open"),
    _claim_with(status="other"),
    _claim_with(by=[]),
    _claim_with(by={"name": "Alex"}),
    _claim_with(by={"id": "member-1"}),
    _claim_with(by="Alex"),
    _claim_without("by"),
    _claim_without("at"),
    _claim_with(at="1794146700"),
    _claim_with(at=float("inf")),
    _claim_with(late=0),
    _claim_without("late"),
]


@pytest.mark.parametrize("body", INVALID_RECEIPTS)
def test_malformed_or_misattributed_receipts_are_rejected_without_replaying_the_post(
    harness: Harness, body: Any
) -> None:
    calls = 0

    def handler(request: Any) -> Any:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        return reply(
            text="null" if body is None else __import__("json").dumps(body, allow_nan=True)
        )

    client = harness.client(handler)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID, "claim"))
    assert caught.value.code in ("invalid_response",)
    assert caught.value.status == 0
    assert calls == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "claim_conflict"),
        (422, "claim_own_request"),
        (403, "forbidden"),
        (404, "claim_not_found"),
    ],
)
def test_refusals_preserve_status_code_and_body_without_replaying(
    harness: Harness, status: int, code: str
) -> None:
    body: Dict[str, Any] = {"code": code, "message": "Refused"}
    if status in (409, 422):
        body.update({"claim": receipt()["claim"], "state": "running"})
    calls = 0

    def handler(request: Any) -> Any:
        nonlocal calls
        calls += 1
        return reply(body, status)

    client = harness.client(handler)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID, "claim"))
    assert (caught.value.status, caught.value.code) == (status, code)
    assert caught.value.body == body
    assert calls == 1


def test_a_conflict_names_who_got_it_first(harness: Harness) -> None:
    claim = {
        "status": "claimed",
        "by": {"id": "u2", "name": "Sam"},
        "at": 1794146700,
        "late": False,
    }
    client = harness.client(
        static(
            {
                "code": "claim_conflict",
                "message": "Sam is already on it.",
                "claim": claim,
                "state": "running",
            },
            409,
        )
    )
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.claim("AB12CD", NOTIFICATION_ID, "claim"))
    assert caught.value.body["claim"]["by"]["name"] == "Sam"


def test_claim_refuses_an_empty_notification_id_and_unsupported_actions_before_any_request(
    harness: Harness,
) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.live.claim("AB12CD", "", "done"))
    assert caught.value.code == "invalid_request"
    bad_actions: List[Any] = ["", "take", "constructor", "__proto__", None, 42, {}]
    for action in bad_actions:
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, a=action: c.live.claim("AB12CD", NOTIFICATION_ID, a))
        assert caught.value.code == "invalid_request"
    assert rec.requests == []


def test_the_claim_error_codes_are_exported() -> None:
    assert list(LIVE_CLAIM_ERROR_CODES) == [
        "claim_conflict",
        "claim_own_request",
        "claim_private_rooms_only",
        "claim_not_found",
        "claim_request_invalid",
        "claim_must_start_running",
        "claim_deadline_out_of_range",
    ]
