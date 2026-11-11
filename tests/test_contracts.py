"""Replays the vendored contract fixtures (``tests/contracts/``) through the SDK.

Laravel writes each fixture from its real server code, and every SDK re-implements
the rules by hand. A disagreement fails here, naming the vector, instead of
surfacing as a 422 in production. Where this SDK and the server disagree today,
the disagreement is pinned exactly in ``KNOWN_DRIFT``: a new one fails the test,
and so does a fixed one still listed. (The list is the TypeScript SDK's, entry for
entry.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from pingroom import (
    MAX_PING_ACTION_BODY_BYTES,
    MAX_PING_ACTION_BODY_KEYS,
    MAX_PING_ACTION_LABEL_LENGTH,
    MAX_PING_ACTION_URL_LENGTH,
    MAX_PING_ACTIONS,
    PING_ACTION_ID_PATTERN,
    PING_ACTION_RUN_WINDOW_HOURS,
    PING_ACTION_STYLES,
    PING_ACTION_TYPES,
    PingRoom,
    PingRoomError,
    link_ping,
    live_status,
    send_incoming_webhook,
    verify_webhook_signature,
)
from pingroom._guards import (
    MAX_PING_TITLE_LENGTH,
    MAX_PRIVATE_PING_MESSAGE_LENGTH,
    MAX_PUBLIC_PING_MESSAGE_LENGTH,
    compact_json,
    server_json_byte_length,
)
from pingroom._ping_actions import (
    RESERVED_PING_ACTION_LABELS,
    clean_ping_action_label,
    ping_action_comparison_form,
    ping_action_url_verdict,
)
from pingroom.live_status import LIVE_STATUS_LIMITS, LIVE_TEMPLATES

CONTRACTS = Path(__file__).parent / "contracts"


def load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# structured-ping-vectors.json
# ---------------------------------------------------------------------------

VECTORS = load("structured-ping-vectors.json")

KNOWN_DRIFT: Dict[str, Any] = {
    # Byte counts that differ although the SDK's measure is the server's formula:
    # TrimStrings / ConvertEmptyStringsToNull run before the 8 KB rule, so "" is
    # measured as null (4 bytes) and surrounding whitespace is not measured.
    "measure": [
        {"id": "empty-string-becomes-null", "check": "serverBytes", "client": 22, "server": 24},
        {
            "id": "empty-string-null-expansion-at-limit",
            "check": "serverBytes",
            "client": 8192,
            "server": 8194,
        },
        {"id": "padded-string-is-trimmed", "check": "serverBytes", "client": 29, "server": 23},
    ],
    # Verdicts that differ, for every sender that takes `data`.
    "data": [
        # The server decodes {"0":..,"1":..} to a PHP list and refuses it as one.
        {"id": "numeric-keys-look-like-list", "check": "accepted", "client": True, "server": False},
        # The empty string becomes null before the 8 KB rule runs: 8192 bytes as sent, 8194 as measured.
        {
            "id": "empty-string-null-expansion-at-limit",
            "check": "accepted",
            "client": True,
            "server": False,
        },
    ],
    # The server accepts any bytes and counts code points. The SDK narrows the key
    # to printable ASCII on purpose, because it travels as an HTTP header: a
    # character outside Latin-1 cannot be sent, and a Latin-1 byte above 0x7E
    # reaches the server as invalid UTF-8. Each of these is "the server would
    # accept it, the SDK refuses it"; none is the other way round.
    "idempotency_key": [
        {"id": key_id, "check": "accepted", "client": False, "server": True}
        for key_id in (
            "form-feed-edges",
            "nbsp-edges",
            "non-ascii-persian",
            "non-ascii-latin1",
            "persian-130-chars",
            "emoji-255-chars",
            "inner-control-char",
            "inner-tab",
            "inner-del",
        )
    ],
    # Only these two senders test the key for truthiness, so an empty string is
    # not refused: the request goes out with no Idempotency-Key at all, where the
    # server would have said 422.
    "idempotency_key_by_sender": {
        "handoffs.request_ack": [
            {"id": "empty", "check": "accepted", "client": "sent without the key", "server": False}
        ],
        "send_incoming_webhook": [
            {"id": "empty", "check": "accepted", "client": "sent without the key", "server": False}
        ],
    },
    "button_label": [],
}


class Sender:
    """One public method, driven through a recording transport."""

    def __init__(self, name: str, send: Callable[[Any, Dict[str, Any]], Any]) -> None:
        self.name = name
        self.send = send


def _webhook(client: Any, extra: Dict[str, Any]) -> Any:
    payload: Dict[str, Any] = {"message": "hi"}
    if "data" in extra:
        payload["data"] = extra["data"]
    options: Dict[str, Any] = {"transport": client.transport}
    if "idempotency_key" in extra:
        options["idempotency_key"] = extra["idempotency_key"]
    return send_incoming_webhook("https://example.test/webhooks/room/secret", payload, **options)


SENDERS: Dict[str, Sender] = {
    "broadcast": Sender("broadcast", lambda c, e: c.pr.broadcast("AB12", "hi", **e)),
    "handoffs.request_ack": Sender(
        "handoffs.request_ack", lambda c, e: c.pr.handoffs.request_ack("hi", **e)
    ),
    "approvals.request": Sender(
        "approvals.request", lambda c, e: c.pr.approvals.request("AB12", "Ship?", **e)
    ),
    "questions.ask": Sender("questions.ask", lambda c, e: c.pr.questions.ask("AB12", "Ship?", **e)),
    "live.push": Sender(
        "live.push",
        lambda c, e: c.pr.live.push(
            "AB12",
            {"correlation_id": "c1", "live_status": {"state": "active", "message": "x"}, **e},
        ),
    ),
    "send_incoming_webhook": Sender("send_incoming_webhook", _webhook),
}
DATA_SENDERS = [
    "broadcast",
    "handoffs.request_ack",
    "approvals.request",
    "questions.ask",
    "live.push",
    "send_incoming_webhook",
]
KEY_SENDERS = [
    "broadcast",
    "handoffs.request_ack",
    "approvals.request",
    "questions.ask",
    "send_incoming_webhook",
]


class Context:
    def __init__(self) -> None:
        self.calls: List[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)
        self.pr = PingRoom("t", transport=self.transport)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(201, json={"id": "x", "state": "pending", "success": True})


def attempt(name: str, extra: Dict[str, Any]) -> Tuple[bool, Any, Any]:
    """Send once. Returns (sent, error, request)."""
    ctx = Context()
    try:
        SENDERS[name].send(ctx, extra)
    except PingRoomError as error:
        assert ctx.calls == [], f"{name}: refused locally but a request was made anyway"
        return False, error, None
    assert len(ctx.calls) == 1, f"{name}: expected exactly one request"
    return True, None, ctx.calls[0]


def data_reason(error: PingRoomError) -> str:
    if error.code != "invalid_data":
        return f"unknown: {error.code} {error.message}"
    if "at most 25 keys" in error.message:
        return "too_many_keys"
    if "too large" in error.message:
        return "too_large"
    if "JSON object" in error.message:
        return "not_object"
    return f"unknown: {error.message}"


def test_the_vector_fixture_is_the_shape_these_tests_read() -> None:
    assert VECTORS["contract"] == "structured-ping-vectors"
    assert VECTORS["version"] == 1
    assert VECTORS["limits"] == {
        "dataMaxBytes": 8192,
        "dataMaxKeys": 25,
        "idempotencyKeyMaxCodePoints": 255,
        "buttonLabelMaxCodePoints": 26,
    }
    for section in ("data", "idempotencyKey", "buttonLabel"):
        assert len(VECTORS[section]) > 5, f"{section} vectors are missing"


def test_data_the_sdk_measures_every_vector_the_way_the_server_does() -> None:
    observed = []
    for vector in VECTORS["data"]:
        if vector["serverBytes"] is None:
            continue
        measured = server_json_byte_length(compact_json(vector["data"]))
        if measured != vector["serverBytes"]:
            observed.append(
                {
                    "id": vector["id"],
                    "check": "serverBytes",
                    "client": measured,
                    "server": vector["serverBytes"],
                }
            )
    assert observed == KNOWN_DRIFT["measure"], VECTORS["regenerate"]


def test_data_every_public_method_that_takes_data_agrees_with_the_server_on_every_vector() -> None:
    observed = []
    for name in DATA_SENDERS:
        for vector in VECTORS["data"]:
            sent, error, request = attempt(name, {"data": vector["data"]})
            if sent != vector["accepted"]:
                observed.append(
                    {
                        "sender": name,
                        "id": vector["id"],
                        "check": "accepted",
                        "client": sent,
                        "server": vector["accepted"],
                    }
                )
            elif sent:
                assert json.loads(request.content)["data"] == vector["data"], (
                    f"{name} {vector['id']}: data changed on the wire"
                )
            else:
                # Both refuse: the SDK must name a reason the server also found.
                reason = data_reason(error)
                assert reason in vector["reasons"], (
                    f"{name} {vector['id']}: the SDK refused as {reason}; the server's reasons are "
                    f"{vector['reasons']}. {VECTORS['regenerate']}"
                )
    expected = [
        {"sender": sender, **entry} for sender in DATA_SENDERS for entry in KNOWN_DRIFT["data"]
    ]
    assert observed == expected, VECTORS["regenerate"]


def test_idempotency_key_every_public_method_agrees_with_the_server_and_sends_the_trimmed_key() -> (
    None
):
    observed = []
    for name in KEY_SENDERS:
        for vector in VECTORS["idempotencyKey"]:
            sent, error, request = attempt(name, {"idempotency_key": vector["key"]})
            # A key the SDK refuses must be a typed error, never a network failure.
            if not sent:
                assert error.code == "invalid_request", f"{name} {vector['id']}: {error.code}"
                assert "idempotency_key" in error.message, f"{name} {vector['id']}"
            header = request.headers.get("idempotency-key") if sent else None
            # True: sent with the key. False: refused. A string: sent, but the key is not on the wire.
            client: Any = (
                False if not sent else ("sent without the key" if header is None else True)
            )
            if client != vector["accepted"]:
                observed.append(
                    {
                        "sender": name,
                        "id": vector["id"],
                        "check": "accepted",
                        "client": client,
                        "server": vector["accepted"],
                    }
                )
            elif client is True and header != vector["trimmed"]:
                observed.append(
                    {
                        "sender": name,
                        "id": vector["id"],
                        "check": "trimmed",
                        "client": header,
                        "server": vector["trimmed"],
                    }
                )

    position = {vector["id"]: index for index, vector in enumerate(VECTORS["idempotencyKey"])}
    expected: List[Dict[str, Any]] = []
    for sender in KEY_SENDERS:
        entries = [
            *KNOWN_DRIFT["idempotency_key"],
            *KNOWN_DRIFT["idempotency_key_by_sender"].get(sender, []),
        ]
        entries.sort(key=lambda entry: position[entry["id"]])
        expected.extend({"sender": sender, **entry} for entry in entries)
    assert observed == expected, VECTORS["regenerate"]


def test_button_label_link_ping_counts_code_points_like_the_server() -> None:
    observed = []
    for vector in VECTORS["buttonLabel"]:
        accepted = True
        built: Any = None
        try:
            built = link_ping("https://example.com/build/512", button_label=vector["label"])
        except ValueError:
            accepted = False
        if accepted != vector["accepted"]:
            observed.append(
                {
                    "id": vector["id"],
                    "check": "accepted",
                    "client": accepted,
                    "server": vector["accepted"],
                }
            )
        elif accepted:
            assert built["button_label"] == vector["label"], f"{vector['id']}: label changed"
    assert observed == KNOWN_DRIFT["button_label"], VECTORS["regenerate"]


# ---------------------------------------------------------------------------
# text-limits.json
# ---------------------------------------------------------------------------

LIMITS = load("text-limits.json")["fields"]

# Where this SDK keeps the limit a fixture entry names.
ENFORCED = {
    "ping.message.private": MAX_PRIVATE_PING_MESSAGE_LENGTH,
    "ping.message.public": MAX_PUBLIC_PING_MESSAGE_LENGTH,
    "ping.title": MAX_PING_TITLE_LENGTH,
}
# Fixture entries about fields this SDK has no method for, or that it leaves to the
# server on purpose (client checks are never stricter than the route behind them).
NOT_ENFORCED = {
    "room.name": "rooms are not managed in 0.1.0",
    "room.description": "rooms are not managed in 0.1.0",
    "room.handle": "rooms are not managed in 0.1.0",
    "room.password.create": "rooms are not managed in 0.1.0",
    "room.password.update": "rooms are not managed in 0.1.0",
    "personal_room.name": "personal rooms are not managed in 0.1.0",
    "personal_room.handle": "personal rooms are not managed in 0.1.0",
    "personal_room.pause_message": "personal rooms are not managed in 0.1.0",
    "contact_share.note": "contact shares are an app feature, not an agent route",
    "qr_image.size": "an app upload, not an agent route",
    "quick_ping.label": "quick actions are not managed in 0.1.0",
    "preset.title": "presets are an app feature, not an agent route",
    "preset.slot_label": "presets are an app feature, not an agent route",
    "question.option_label": "left to the server (the SDK never checks option text)",
    "agent.label": "agent registration is not wrapped in 0.1.0",
}


def test_every_text_limit_in_the_fixture_is_enforced_or_excused() -> None:
    assert set(ENFORCED) | set(NOT_ENFORCED) == set(LIMITS), (
        "text-limits.json has a field this SDK neither enforces nor excuses. "
        "Add it to ENFORCED (and enforce it) or to NOT_ENFORCED with a reason."
    )
    assert not set(ENFORCED) & set(NOT_ENFORCED)


@pytest.mark.parametrize("field,limit", sorted(ENFORCED.items()))
def test_the_sdk_text_limits_are_the_servers(field: str, limit: int) -> None:
    entry = LIMITS[field]
    assert entry["unit"] == "code_points"
    assert limit == entry["max"], f"{field}: the SDK has {limit}, the server {entry['max']}"


def test_message_and_title_limits_count_code_points_at_the_wire() -> None:
    # Public rooms take 160 and private 120; the invite code does not say which, so the SDK
    # checks the 160 ceiling and the server applies 120 to a private room.
    def sends(message: str, title: Optional[str] = None) -> bool:
        ctx = Context()
        try:
            ctx.pr.broadcast("AB12", message, title=title)
        except PingRoomError:
            return False
        return True

    emoji = "\U0001f600"
    assert sends(emoji * LIMITS["ping.message.public"]["max"])
    assert not sends(emoji * (LIMITS["ping.message.public"]["max"] + 1))
    assert sends("m", emoji * LIMITS["ping.title"]["max"])
    assert not sends("m", emoji * (LIMITS["ping.title"]["max"] + 1))
    # The webhook helper holds the same ceilings.
    for message, ok in (("m" * 160, True), ("m" * 161, False)):
        ctx = Context()
        try:
            send_incoming_webhook(
                "https://x.example/h", {"message": message}, transport=ctx.transport
            )
            assert ok
        except PingRoomError:
            assert not ok


# ---------------------------------------------------------------------------
# ping-actions.json
# ---------------------------------------------------------------------------

ACTIONS = load("ping-actions.json")


def test_ping_action_limits_are_the_servers() -> None:
    assert {
        "max_actions": MAX_PING_ACTIONS,
        "id_pattern": f"^{PING_ACTION_ID_PATTERN.pattern}$",
        "label_max": MAX_PING_ACTION_LABEL_LENGTH,
        "url_max": MAX_PING_ACTION_URL_LENGTH,
        "body_max_bytes": MAX_PING_ACTION_BODY_BYTES,
        "body_max_keys": MAX_PING_ACTION_BODY_KEYS,
        "run_window_hours": PING_ACTION_RUN_WINDOW_HOURS,
        "types": list(PING_ACTION_TYPES),
        "styles": list(PING_ACTION_STYLES),
    } == ACTIONS["limits"]


def test_the_sdk_refuses_exactly_the_servers_reserved_labels() -> None:
    assert list(RESERVED_PING_ACTION_LABELS) == ACTIONS["reserved_labels"]
    for label in ACTIONS["reserved_labels"]:
        assert ping_action_comparison_form(label) == label, f"{label} is not in its comparison form"


def test_every_label_vector_gets_the_servers_verdict_and_cleaned_text() -> None:
    assert len(ACTIONS["label_vectors"]) > 10
    observed = []
    for vector in ACTIONS["label_vectors"]:
        verdict = clean_ping_action_label(vector["label"])
        accepted = verdict.error is None
        if accepted != vector["accepted"]:
            observed.append(
                {
                    "label": vector["label"],
                    "check": "accepted",
                    "client": accepted,
                    "server": vector["accepted"],
                }
            )
        elif accepted and verdict.label != vector["cleaned"]:
            observed.append(
                {
                    "label": vector["label"],
                    "check": "cleaned",
                    "client": verdict.label,
                    "server": vector["cleaned"],
                }
            )
        elif not accepted and verdict.error != vector["reason"]:
            observed.append(
                {
                    "label": vector["label"],
                    "check": "reason",
                    "client": verdict.error,
                    "server": vector["reason"],
                }
            )
    assert observed == [], (
        "Regenerate the fixture only from Laravel: "
        + "WRITE_PING_ACTIONS_CONTRACT=1 php artisan test --compact tests/Unit/PingActionsContractTest.php"
    )


def test_every_url_vector_gets_the_servers_verdict_and_host() -> None:
    assert len(ACTIONS["url_vectors"]) > 10
    observed = []
    for vector in ACTIONS["url_vectors"]:
        verdict = ping_action_url_verdict(vector["url"], vector["type"])
        accepted = verdict.error is None
        if accepted != vector["accepted"]:
            observed.append(
                {
                    "url": vector["url"],
                    "type": vector["type"],
                    "check": "accepted",
                    "client": accepted,
                    "server": vector["accepted"],
                }
            )
        elif accepted and verdict.host != vector["host"]:
            observed.append(
                {
                    "url": vector["url"],
                    "type": vector["type"],
                    "check": "host",
                    "client": verdict.host,
                    "server": vector["host"],
                }
            )
        elif not accepted and verdict.error != vector["reason"]:
            observed.append(
                {
                    "url": vector["url"],
                    "type": vector["type"],
                    "check": "reason",
                    "client": verdict.error,
                    "server": vector["reason"],
                }
            )
    assert observed == []


def hkdf_sha256(key_material: bytes, info: bytes, length: int, salt: bytes = b"") -> bytes:
    """RFC 5869, as PHP's ``hash_hkdf('sha256', $ikm, $length, $info, '')`` computes it."""
    pseudo_random_key = hmac.new(salt or b"\x00" * 32, key_material, hashlib.sha256).digest()
    output, block, counter = b"", b"", 1
    while len(output) < length:
        block = hmac.new(
            pseudo_random_key, block + info + bytes([counter]), hashlib.sha256
        ).digest()
        output += block
        counter += 1
    return output[:length]


def test_the_documented_signing_vector_derives_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-Ping secret a button delivery is signed with.

    Laravel derives it from the JWT secret and the notification id; the SDK never
    does (the send result carries ``action_signing_secret``), but a delivery signed
    with it must verify through ``verify_webhook_signature`` exactly like any
    outgoing webhook.
    """
    signing = ACTIONS["signing"]
    assert signing["vectors"]
    for vector in signing["vectors"]:
        key = hkdf_sha256(vector["jwt_secret"].encode(), signing["info"].encode(), 32)
        assert key.hex() == vector["key_hex"]
        secret = hmac.new(key, vector["notification_id"].encode(), hashlib.sha256).hexdigest()
        assert secret == vector["secret"]

        body = json.dumps(
            {
                "event": "notification.action",
                "notification_id": vector["notification_id"],
                "action_id": "clean",
            }
        ).encode()
        timestamp, delivery = "1790000000", "6c5b1a2e-0000-4000-8000-00000000000a"
        monkeypatch.setattr("pingroom._clock.now", lambda: 1790000000.0)
        signature = hmac.new(
            secret.encode(), f"v2\n{timestamp}\n{delivery}\n".encode() + body, hashlib.sha256
        ).hexdigest()
        assert verify_webhook_signature(
            payload=body,
            secret=secret,
            signature_v2=signature,
            timestamp=timestamp,
            delivery_id=delivery,
            require_v2=True,
        )
        assert not verify_webhook_signature(
            payload=body + b" ",
            secret=secret,
            signature_v2=signature,
            timestamp=timestamp,
            delivery_id=delivery,
            require_v2=True,
        )


# ---------------------------------------------------------------------------
# live-templates.json
# ---------------------------------------------------------------------------

TEMPLATES = load("live-templates.json")


def test_live_templates_is_the_fixture_list_names_and_order() -> None:
    assert list(LIVE_TEMPLATES) == TEMPLATES["templates"], TEMPLATES["regenerate"]


def test_every_fixture_template_has_a_builder_that_stamps_it() -> None:
    for template in TEMPLATES["templates"]:
        builder = getattr(live_status, template, None)
        assert callable(builder), f"live_status.{template}() is missing"
        assert builder("c", {"state": "running"})["live_status"]["template"] == template


def test_the_template_an_unknown_one_folds_back_onto_is_a_template_the_sdk_lists() -> None:
    assert TEMPLATES["fallback_template"] in LIVE_TEMPLATES


def test_every_field_a_template_owns_survives_a_builder_round_trip() -> None:
    for template, fields in TEMPLATES["template_fields"].items():
        for field in fields:
            assert field in TEMPLATES["fields"], (
                f"live-templates.json owns {template}.{field} but gives it no limits"
            )
            body = getattr(live_status, template)("c", {"state": "running", field: "x"})
            assert body["live_status"][field] == "x", f"live_status.{template}() dropped {field}"


def test_the_limits_the_sdk_exports_are_the_fixture_limits() -> None:
    fields = TEMPLATES["fields"]
    assert dict(LIVE_STATUS_LIMITS) == {
        "value_length": fields["value"]["max"],
        "value_label_length": fields["value_label"]["max"],
        "delta_length": fields["delta"]["max"],
        "stats_min": fields["stats"]["min"],
        "stats_max": fields["stats"]["max"],
        "stat_label_length": fields["stats"]["item"]["label"]["max"],
        "stat_value_length": fields["stats"]["item"]["value"]["max"],
        "segments_min": fields["segments"]["min"],
        "segments_max": fields["segments"]["max"],
    }, TEMPLATES["regenerate"]


def test_the_immutable_fields_are_the_ones_the_sdk_documents() -> None:
    # steps (steps_immutable), segments (segments_immutable), started_at
    # (started_at_immutable). The SDK carries these in the live_status docstrings,
    # so a new immutable field means a doc change there first.
    assert TEMPLATES["immutable_fields"] == {
        "steps": ["steps"],
        "progress": ["segments"],
        "timer": ["started_at"],
    }
    doc = live_status.__doc__ or ""
    for code in ("steps_immutable", "segments_immutable", "started_at_immutable"):
        assert code in doc


def test_reserved_labels_in_the_comparison_form_are_ascii_lowercase_or_letters() -> None:
    for label in ACTIONS["reserved_labels"]:
        assert not re.search(r"\s", label)
