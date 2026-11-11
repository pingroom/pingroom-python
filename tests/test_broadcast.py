"""``broadcast()``: the exact request, the local checks, idempotency, replace keys and buttons.

Expectations come from the TypeScript SDK's own tests (broadcast, replace, tags,
ping-actions and client-hardening).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from conftest import Harness, Recorder, raw_path, static
from pingroom import PING_ACTION_ERROR_CODES, PingRoomError, link_ping, location_ping, ping_action

PATH = "/api/agent/rooms/ab12/notifications"
PING = {
    "id": "n1",
    "message": "hi",
    "created_at": "now",
    "action_number": None,
    "action_icon": None,
    "trigger_source": None,
}


def invalid(error: PingRoomError, code: str = "invalid_request") -> bool:
    return isinstance(error, PingRoomError) and error.code == code and error.status == 0


def test_broadcast_posts_the_ping_body_to_the_right_path(harness: Harness) -> None:
    rec = Recorder(routes={f"POST {PATH}": static(PING, 201)})
    client = harness.client(rec)
    result = harness.run(
        client,
        lambda c: c.broadcast(
            "ab12",
            "hi",
            data={"commit": "abc"},
            correlation_id="c1",
            requires_ack=True,
            ack_timeout_seconds=300,
        ),
    )
    assert result["id"] == "n1"
    assert rec.last.method == "POST"
    assert rec.last.url.path == PATH
    assert rec.body() == {
        "message": "hi",
        "data": {"commit": "abc"},
        "correlation_id": "c1",
        "requires_ack": True,
        "ack_timeout_seconds": 300,
    }
    assert "idempotency-key" not in rec.last.headers


def test_broadcast_sends_only_the_fields_that_are_set(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "hi"))
    assert rec.body() == {"message": "hi"}


def test_every_field_reaches_the_wire_under_its_api_name(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.broadcast(
            "ab12",
            "Build 41 green",
            title="Deploy",
            data={"k": "v"},
            tags=["ci", "main"],
            correlation_id="corr",
            reply_to="reply",
            action_number=3,
            action_icon="rocket",
            attachment_ids=["a1", "a2", "a3", "a4"],
            requires_ack=True,
            ack_mode="all",
            is_urgent=True,
            ack_timeout_seconds=120,
            replace_key="build:41",
            idempotency_key="k-1",
        ),
    )
    assert rec.body() == {
        "message": "Build 41 green",
        "title": "Deploy",
        "action_number": 3,
        "action_icon": "rocket",
        "data": {"k": "v"},
        "correlation_id": "corr",
        "reply_to": "reply",
        "tags": ["ci", "main"],
        "attachment_ids": ["a1", "a2", "a3", "a4"],
        "requires_ack": True,
        "ack_mode": "all",
        "is_urgent": True,
        "ack_timeout_seconds": 120,
        "replace_key": "build:41",
    }
    # The Python and camelCase SDK-only names never reach the wire.
    assert "idempotency_key" not in rec.body()
    assert rec.last.headers["idempotency-key"] == "k-1"


def test_is_urgent_does_not_imply_requires_ack(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "prod is down", is_urgent=True))
    assert rec.body() == {"message": "prod is down", "is_urgent": True}


def test_false_values_are_sent_not_dropped(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "hi", requires_ack=False, is_urgent=False))
    assert rec.body() == {"message": "hi", "requires_ack": False, "is_urgent": False}


def test_the_invite_code_is_url_encoded(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab 12/x", "hi"))
    assert raw_path(rec.last) == b"/api/agent/rooms/ab%2012%2Fx/notifications"


# --- text limits ----------------------------------------------------------


def test_public_room_boundaries_are_accepted_and_the_title_is_forwarded(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    message, title = "m" * 160, "t" * 40
    harness.run(client, lambda c: c.broadcast("public1", message, title=title))
    assert rec.body() == {"message": message, "title": title}


def test_only_the_public_ceiling_is_checked_locally(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "m" * 161))
    assert invalid(caught.value)
    assert "message" in caught.value.message and "160" in caught.value.message
    assert "161" in caught.value.message
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "ok", title="t" * 41))
    assert invalid(caught.value)
    assert "title" in caught.value.message and "40" in caught.value.message
    assert rec.requests == []
    # 121 to 160 is left to the server (a private room refuses it, a public one accepts).
    harness.run(client, lambda c: c.broadcast("ab12", "m" * 121))
    assert len(rec.requests) == 1


def test_text_is_counted_in_code_points_not_utf16_units(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "\U0001f600" * 160))
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.broadcast("ab12", "\U0001f600" * 161))


@pytest.mark.parametrize("message", ["", "   ", None, 5])
def test_a_missing_message_is_refused(harness: Harness, message: Any) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", message))
    assert invalid(caught.value)
    assert "message" in caught.value.message


@pytest.mark.parametrize("number", [0, 17, -1, 1.5, "3", True])
def test_the_action_slot_is_an_integer_from_1_to_16(harness: Harness, number: Any) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", action_number=number))
    assert invalid(caught.value)


def test_an_integral_float_action_slot_is_sent_as_an_integer(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "hi", action_number=16.0))
    assert rec.last.content == b'{"message":"hi","action_number":16}'


# --- structured data ------------------------------------------------------


def test_data_is_measured_the_way_the_server_measures_it(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    # 2,000 Persian characters are 4 KB of UTF-8 but 12 KB of json_encode output.
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", data={"note": "س" * 2000}))
    assert invalid(caught.value, "invalid_data")
    # json_encode escapes every slash, so 4,100 of them pass 8 KB too.
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", data={"path": "/" * 4100}))
    assert invalid(caught.value, "invalid_data")
    assert rec.requests == []
    harness.run(client, lambda c: c.broadcast("ab12", "hi", data={"note": "a" * 8000}))
    assert len(rec.requests) == 1


def test_astral_characters_count_as_two_utf16_units(harness: Harness) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    # One emoji is 12 bytes of 😀 escapes: 8192 // 12 = 682 fit once the key is counted.
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.broadcast("ab12", "hi", data={"n": "\U0001f600" * 700}))
    harness.run(client, lambda c: c.broadcast("ab12", "hi", data={"n": "\U0001f600" * 680}))


@pytest.mark.parametrize("data", [[1], "x", 5, True])
def test_data_must_be_a_json_object(harness: Harness, data: Any) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", data=data))
    assert invalid(caught.value, "invalid_data")


def test_data_may_have_25_keys_not_26(harness: Harness) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    harness.run(client, lambda c: c.broadcast("ab12", "hi", data={f"k{i}": i for i in range(25)}))
    with pytest.raises(PingRoomError) as caught:
        harness.run(
            client, lambda c: c.broadcast("ab12", "hi", data={f"k{i}": i for i in range(26)})
        )
    assert "at most 25 keys" in caught.value.message


def test_data_that_is_not_json_is_refused(harness: Harness) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    for bad in ({"a": float("nan")}, {"a": object()}, {"a": float("inf")}):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, d=bad: c.broadcast("ab12", "hi", data=d))
        assert invalid(caught.value, "invalid_data")


def test_link_and_location_fragments_ride_in_data(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(
        client,
        lambda c: c.broadcast(
            "ab12",
            "Meet here",
            data={
                "event": "lunch",
                **link_ping("https://ci.example.com/b/512", button_label="Open build"),
                **location_ping(25.2048, 55.2708, label="Dubai Mall"),
            },
        ),
    )
    assert rec.body()["data"] == {
        "event": "lunch",
        "url": "https://ci.example.com/b/512",
        "button_label": "Open build",
        "location": {"latitude": 25.2048, "longitude": 55.2708, "label": "Dubai Mall"},
    }


# --- idempotency ----------------------------------------------------------


def test_the_idempotency_key_is_a_header_never_a_body_field(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "x"}, 201))
    client = harness.client(rec)
    harness.run(
        client, lambda c: c.broadcast("AB12", "Deploy shipped", idempotency_key="deploy-1.4.0")
    )
    harness.run(client, lambda c: c.broadcast("AB12", "Deploy shipped"))
    assert rec.requests[0].headers["idempotency-key"] == "deploy-1.4.0"
    assert rec.body(0) == {"message": "Deploy shipped"}
    assert "idempotency-key" not in rec.requests[1].headers
    assert rec.body(1) == {"message": "Deploy shipped"}


@pytest.mark.parametrize("key", ["", "   ", "k" * 256])
def test_an_empty_or_overlong_key_is_refused_before_sending(harness: Harness, key: str) -> None:
    rec = Recorder(fallback=static({"id": "x"}, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("AB12", "hi", idempotency_key=key))
    assert invalid(caught.value)
    assert "idempotency_key" in caught.value.message
    assert rec.requests == []


@pytest.mark.parametrize(
    "key",
    ["deploy-✅", "ключ", "café", "line\nbreak", "tab\there", "nul\x00inside", "del\x7f", 42],
)
def test_a_key_that_cannot_travel_as_a_header_is_invalid_request_not_a_network_error(
    harness: Harness, key: Any
) -> None:
    rec = Recorder(fallback=static({"id": "x"}, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("AB12", "hi", idempotency_key=key))
    assert invalid(caught.value)
    assert "idempotency_key" in caught.value.message
    assert rec.requests == []


def test_a_key_is_trimmed_the_way_the_server_trims_it(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "x"}, 201))
    client = harness.client(rec)
    printable = "".join(chr(code) for code in range(0x21, 0x7F))
    for key in (
        "deploy 1.4.0 abc123",
        " \t deploy-2\n",
        " " + "k" * 255 + " ",
        "a " + printable,
    ):
        harness.run(client, lambda c, k=key: c.broadcast("AB12", "hi", idempotency_key=k))
    assert [r.headers["idempotency-key"] for r in rec.requests] == [
        "deploy 1.4.0 abc123",
        "deploy-2",
        "k" * 255,
        "a " + printable,
    ]


# --- replace_key ----------------------------------------------------------


def test_replace_key_is_sent_as_replace_key_and_replaced_reads_back(harness: Harness) -> None:
    replaced = {**PING, "replaced": True, "edited_at": "2026-11-11T12:00:00.000000Z"}
    rec = Recorder(fallback=static(replaced, 200))
    client = harness.client(rec)
    result = harness.run(
        client,
        lambda c: c.broadcast(
            "ab12", "Build 41: green", replace_key="build:41", idempotency_key="k-1"
        ),
    )
    assert result["replaced"] is True
    assert result["edited_at"] == "2026-11-11T12:00:00.000000Z"
    assert rec.body() == {"message": "Build 41: green", "replace_key": "build:41"}
    assert rec.last.headers["idempotency-key"] == "k-1"


@pytest.mark.parametrize("key", ["", "has space", "ünï", "a/b", "x" * 65, 7])
def test_a_replace_key_is_checked_before_any_request(harness: Harness, key: Any) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", replace_key=key))
    assert invalid(caught.value)
    assert rec.requests == []


@pytest.mark.parametrize("key", ["a", "x" * 64, "build.41_prod:eu-1", "A9"])
def test_valid_replace_keys_are_accepted(harness: Harness, key: str) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", "hi", replace_key=key))
    assert rec.body()["replace_key"] == key


# --- tags -----------------------------------------------------------------


def test_tags_are_sent_as_given_and_read_back(harness: Harness) -> None:
    rec = Recorder(fallback=static({**PING, "tags": ["deploy"]}, 201))
    client = harness.client(rec)
    result = harness.run(
        client, lambda c: c.broadcast("ab12", "hi", tags=["Deploy ", "deploy", "prod_1"])
    )
    assert result["tags"] == ["deploy"]
    # The server normalises; the SDK does not rewrite the payload.
    assert rec.body()["tags"] == ["Deploy ", "deploy", "prod_1"]


TAG_32 = "a" * 32


@pytest.mark.parametrize(
    "tags",
    [
        ["bad tag"],
        ["a.b"],
        ["café"],
        ["-lead"],
        ["_lead"],
        [TAG_32 + "a"],
        [f"t{i}" for i in range(11)],
        "deploy",
        [5],
        [None],
        [["deploy"]],
    ],
)
def test_tags_the_server_would_refuse_are_refused_first(harness: Harness, tags: Any) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=tags))
    assert invalid(caught.value)
    assert rec.requests == []


def test_tag_limits_and_what_the_server_normalises_away(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)
    ten = [str(i) * 32 for i in range(10)]
    harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=ten))
    # Eleven that collapse to ten once trimmed, lowercased and de-duplicated.
    harness.run(
        client, lambda c: c.broadcast("ab12", "hi", tags=[*(f"t{i}" for i in range(10)), " T0 "])
    )
    # Blanks are dropped, as on the server.
    harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=["", "  ", "ok"]))
    harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=[]))
    harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=None))
    harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=(TAG_32, "0", "a-b_c")))
    assert len(rec.requests) == 6
    assert rec.body(3)["tags"] == []
    assert "tags" not in rec.body(4)
    assert rec.body(5)["tags"] == [TAG_32, "0", "a-b_c"]


def test_tag_trimming_is_ascii_lowercase_only(harness: Harness) -> None:
    client = harness.client(Recorder(fallback=static(PING, 201)))
    # A non-ASCII capital never folds into a valid tag (PHP's strtolower is ASCII only).
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=["İ"]))
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.broadcast("ab12", "hi", tags=["K"]))


# --- buttons --------------------------------------------------------------

OPEN: Dict[str, Any] = {
    "label": "Open dashboard",
    "type": "open_url",
    "url": "https://grafana.example.com/d/disk",
}
HOOK: Dict[str, Any] = {
    "id": "clean",
    "label": "Clean up now",
    "type": "webhook",
    "url": "https://ops.example.com/cleanup",
    "body": {"host": "db-2"},
    "style": "danger",
}


def test_broadcast_sends_buttons_in_data_actions_and_resolves_with_the_signing_secret(
    harness: Harness,
) -> None:
    response = {
        **PING,
        "actions": [
            {**OPEN, "id": "a1", "style": "default", "host": "grafana.example.com"},
            {"id": "clean", "label": "Clean up now", "type": "webhook", "style": "danger"},
        ],
        "actions_expire_at": "2026-11-12T10:00:00+00:00",
        "action_runs": [],
        "action_signing_secret": "ab" * 32,
    }
    rec = Recorder(fallback=static(response, 201))
    client = harness.client(rec)
    result = harness.run(
        client,
        lambda c: c.broadcast(
            "AB12",
            "Disk 91%",
            data={
                "actions": [
                    ping_action.open_url(OPEN["label"], OPEN["url"]),
                    ping_action.webhook(
                        HOOK["label"],
                        HOOK["url"],
                        id="clean",
                        style="danger",
                        body={"host": "db-2"},
                    ),
                ]
            },
        ),
    )
    assert rec.body()["data"]["actions"] == [
        OPEN,
        {
            "label": "Clean up now",
            "type": "webhook",
            "url": HOOK["url"],
            "id": "clean",
            "style": "danger",
            "body": {"host": "db-2"},
        },
    ]
    assert result["action_signing_secret"] == "ab" * 32
    assert "url" not in result["actions"][1]


def test_invalid_raw_buttons_are_refused_naming_every_field(harness: Harness) -> None:
    rec = Recorder(fallback=static(PING, 201))
    client = harness.client(rec)

    def send(actions: Any) -> Any:
        return lambda c: c.broadcast("AB12", "x", data={"actions": actions})

    cases = [
        (
            [{"label": "paypal.com", "type": "open_url", "url": "http://x"}],
            r"data\.actions\.0\.label: .*data\.actions\.0\.url:",
        ),
        ([OPEN, OPEN, OPEN, OPEN], "1 to 3 buttons"),
        (
            [{**OPEN, "id": "x"}, {**OPEN, "id": "x"}],
            r"data\.actions\.1\.id: Each button on a Ping needs its own id",
        ),
        ([{**OPEN, "method": "POST"}], "Unknown button field: method"),
        ({}, "must be a list"),
    ]
    import re

    for actions, pattern in cases:
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, send(actions))
        assert invalid(caught.value, "invalid_data")
        assert re.search(pattern, caught.value.message), caught.value.message
    assert rec.requests == []
    # `None` is absent, as on the server.
    harness.run(client, send(None))
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    "status,code",
    [(422, c) for c in PING_ACTION_ERROR_CODES[:3]] + [(403, PING_ACTION_ERROR_CODES[3])],
)
def test_every_button_refusal_surfaces_its_status_and_code(
    harness: Harness, status: int, code: str
) -> None:
    client = harness.client(static({"code": code, "message": code}, status))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("AB12", "x", data={"actions": [OPEN]}))
    assert (caught.value.status, caught.value.code) == (status, code)
