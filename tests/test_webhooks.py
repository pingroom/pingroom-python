"""``verify_webhook_signature`` and ``send_incoming_webhook`` (sync and async)."""

from __future__ import annotations

import asyncio
import hmac
import json
from hashlib import sha256
from typing import Any, Callable, Dict, List

import httpx
import pytest

from conftest import FakeClock, Harness, reply
from pingroom import (
    INCOMING_WEBHOOK_FIELDS,
    PingRoomError,
    PingRoomNetworkError,
    PingRoomTimeoutError,
    send_incoming_webhook,
    send_incoming_webhook_async,
    verify_webhook_signature,
)

SECRET = "whsec_test_123"
RAW_BODY = json.dumps({"event": "ping", "notification_id": "n1", "message": "hi"})
DELIVERY_ID = "delivery-test-1"


def sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, sha256).hexdigest()


@pytest.fixture
def frozen(clock: FakeClock) -> FakeClock:
    return clock


def stamp(clock: FakeClock) -> str:
    return str(int(clock.now()))


def v1(clock: FakeClock, body: str = RAW_BODY, secret: str = SECRET) -> str:
    return sign(secret, f"{stamp(clock)}.{body}".encode())


def v2(
    clock: FakeClock, body: str = RAW_BODY, secret: str = SECRET, delivery: str = DELIVERY_ID
) -> str:
    return sign(secret, f"v2\n{stamp(clock)}\n{delivery}\n{body}".encode())


# --- verify ---------------------------------------------------------------


def test_a_valid_v1_signature_with_or_without_the_prefix(frozen: FakeClock) -> None:
    ts = stamp(frozen)
    payload = RAW_BODY.encode()
    assert verify_webhook_signature(
        payload=payload, signature=f"sha256={v1(frozen)}", timestamp=ts, secret=SECRET
    )
    assert verify_webhook_signature(
        payload=payload, signature=v1(frozen), timestamp=ts, secret=SECRET
    )
    # The hex is case-insensitive.
    assert verify_webhook_signature(
        payload=payload, signature=v1(frozen).upper(), timestamp=ts, secret=SECRET
    )
    assert verify_webhook_signature(
        payload=payload, signature=f"  {v1(frozen)}  ", timestamp=ts, secret=SECRET
    )


def test_a_valid_v2_signature_binds_the_delivery_id(frozen: FakeClock) -> None:
    ts = stamp(frozen)
    payload = RAW_BODY.encode()
    kwargs: Dict[str, Any] = {"payload": payload, "timestamp": ts, "secret": SECRET}
    assert verify_webhook_signature(**kwargs, signature_v2=v2(frozen), delivery_id=DELIVERY_ID)
    assert not verify_webhook_signature(
        **kwargs, signature_v2=v2(frozen), delivery_id="delivery-tampered"
    )


def test_a_signature_made_under_the_other_scheme_never_verifies(frozen: FakeClock) -> None:
    """A v2 signature is not a v1 signature, and the other way round."""
    ts = stamp(frozen)
    common: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "timestamp": ts,
        "delivery_id": DELIVERY_ID,
        "secret": SECRET,
    }
    assert not verify_webhook_signature(**common, signature=v2(frozen))  # v2 value as v1
    assert not verify_webhook_signature(**common, signature_v2=v1(frozen))  # v1 value as v2
    # Neither may a signature over the right body under a different secret or delivery id pass.
    assert not verify_webhook_signature(**common, signature_v2=v2(frozen, secret="other"))
    assert not verify_webhook_signature(**common, signature_v2=v2(frozen, delivery="another-id"))


def test_a_supplied_v2_that_fails_never_downgrades_to_a_valid_v1(frozen: FakeClock) -> None:
    assert not verify_webhook_signature(
        payload=RAW_BODY.encode(),
        signature=v1(frozen),
        signature_v2="0" * 64,
        timestamp=stamp(frozen),
        delivery_id=DELIVERY_ID,
        secret=SECRET,
    )


def test_changing_only_the_delivery_id_invalidates_v2_but_not_v1(frozen: FakeClock) -> None:
    common: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": v1(frozen),
        "timestamp": stamp(frozen),
        "delivery_id": "delivery-tampered",
        "secret": SECRET,
    }
    assert verify_webhook_signature(**common)
    assert not verify_webhook_signature(**common, signature_v2=v2(frozen))


@pytest.mark.parametrize("delivery", [None, "", "delivery\ninjected", "d" * 256, "sp ace"])
def test_v2_needs_a_safe_delivery_id(frozen: FakeClock, delivery: Any) -> None:
    assert not verify_webhook_signature(
        payload=RAW_BODY.encode(),
        signature=v1(frozen),
        signature_v2=v2(frozen),
        timestamp=stamp(frozen),
        delivery_id=delivery,
        secret=SECRET,
    )


def test_a_delivery_id_of_exactly_255_visible_characters_is_accepted(frozen: FakeClock) -> None:
    delivery = "d" * 255
    assert verify_webhook_signature(
        payload=RAW_BODY.encode(),
        signature_v2=v2(frozen, delivery=delivery),
        timestamp=stamp(frozen),
        delivery_id=delivery,
        secret=SECRET,
    )


def test_every_kind_of_raw_body_is_read_as_bytes(frozen: FakeClock) -> None:
    data = RAW_BODY.encode()
    padded = bytearray(b"\x00" * 8 + data + b"\x00" * 8)
    wide = bytearray(data + b" " * (len(data) % 2))
    for payload in (
        data,
        bytearray(data),
        memoryview(data),
        memoryview(padded)[8 : 8 + len(data)],  # a window onto a larger buffer
    ):
        kwargs: Dict[str, Any] = {
            "payload": payload,
            "timestamp": stamp(frozen),
            "delivery_id": DELIVERY_ID,
            "secret": SECRET,
            "require_v2": True,
        }
        assert verify_webhook_signature(**kwargs, signature_v2=v2(frozen))
        assert not verify_webhook_signature(**kwargs, signature_v2="0" * 64)
    # A memoryview over wider items is read as its bytes, never element by element.
    wide_view = memoryview(wide).cast("H")
    expected = RAW_BODY.encode() + b" " * (len(data) % 2)
    assert verify_webhook_signature(
        payload=wide_view,
        signature_v2=sign(SECRET, f"v2\n{stamp(frozen)}\n{DELIVERY_ID}\n".encode() + expected),
        timestamp=stamp(frozen),
        delivery_id=DELIVERY_ID,
        secret=SECRET,
    )


def test_non_utf8_raw_bytes_are_preserved(frozen: FakeClock) -> None:
    body = bytes([0xFF, 0xFE, 0x00, 0x61])
    ts = stamp(frozen)
    sig1 = sign(SECRET, f"{ts}.".encode() + body)
    sig2 = sign(SECRET, f"v2\n{ts}\n{DELIVERY_ID}\n".encode() + body)
    assert verify_webhook_signature(payload=body, signature=sig1, timestamp=ts, secret=SECRET)
    assert verify_webhook_signature(
        payload=body, signature_v2=sig2, timestamp=ts, delivery_id=DELIVERY_ID, secret=SECRET
    )


def test_a_tampered_body_is_rejected(frozen: FakeClock) -> None:
    assert not verify_webhook_signature(
        payload=(RAW_BODY + " ").encode(),
        signature=v1(frozen),
        timestamp=stamp(frozen),
        secret=SECRET,
    )


def test_a_tampered_or_missing_timestamp_is_rejected(frozen: FakeClock) -> None:
    kwargs: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": v1(frozen),
        "secret": SECRET,
    }
    assert not verify_webhook_signature(**kwargs, timestamp=str(int(stamp(frozen)) - 1))
    for bad in (None, "", "abc", "12.5", "-5", "1e9", "١٢", f"{2**53}"):
        assert not verify_webhook_signature(**kwargs, timestamp=bad)


def test_the_wrong_or_empty_secret_is_rejected(frozen: FakeClock) -> None:
    kwargs: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": v1(frozen),
        "timestamp": stamp(frozen),
    }
    assert not verify_webhook_signature(**kwargs, secret="nope")
    assert not verify_webhook_signature(**kwargs, secret="")
    assert verify_webhook_signature(**kwargs, secret=SECRET.encode())  # bytes secret


@pytest.mark.parametrize(
    "signature", [None, "", "sha256=zzzz", "0" * 63, "0" * 65, "sha256=" + "g" * 64]
)
def test_missing_or_malformed_signatures_are_rejected(frozen: FakeClock, signature: Any) -> None:
    assert not verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=signature, timestamp=stamp(frozen), secret=SECRET
    )
    if signature is not None:  # an absent v2 falls back to a valid v1; a malformed one never does
        assert not verify_webhook_signature(
            payload=RAW_BODY.encode(),
            signature=v1(frozen),
            signature_v2=signature,
            timestamp=stamp(frozen),
            delivery_id=DELIVERY_ID,
            secret=SECRET,
        )


def test_the_replay_window_is_300_seconds_by_default_and_configurable(frozen: FakeClock) -> None:
    old = int(frozen.now()) - 301
    sig = sign(SECRET, f"{old}.{RAW_BODY}".encode())
    kwargs: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": sig,
        "timestamp": str(old),
        "secret": SECRET,
    }
    assert not verify_webhook_signature(**kwargs)
    assert verify_webhook_signature(**kwargs, max_age_seconds=600)
    # The window is two-sided: a timestamp from the future is as stale.
    future = int(frozen.now()) + 301
    future_sig = sign(SECRET, f"{future}.{RAW_BODY}".encode())
    assert not verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=future_sig, timestamp=str(future), secret=SECRET
    )
    # Exactly at the limit still verifies; a nonsense window never does.
    edge = int(frozen.now()) - 300
    edge_sig = sign(SECRET, f"{edge}.{RAW_BODY}".encode())
    assert verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=edge_sig, timestamp=str(edge), secret=SECRET
    )
    bad_windows: List[Any] = [-1, float("nan"), float("inf"), True, "300"]
    for bad in bad_windows:
        assert not verify_webhook_signature(**kwargs, max_age_seconds=bad)


def test_the_timestamp_may_be_an_integer(frozen: FakeClock) -> None:
    assert verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=v1(frozen), timestamp=int(stamp(frozen)), secret=SECRET
    )


def test_require_v2_refuses_a_delivery_that_carries_only_v1(frozen: FakeClock) -> None:
    # The replay the option closes: a captured body re-sent without its v2
    # header and under a new delivery id still passes v1.
    replay: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": v1(frozen),
        "timestamp": stamp(frozen),
        "delivery_id": "delivery-replayed",
        "secret": SECRET,
    }
    assert verify_webhook_signature(**replay)
    assert not verify_webhook_signature(**replay, require_v2=True)
    assert not verify_webhook_signature(**replay, signature_v2=None, require_v2=True)


def test_require_v2_accepts_a_valid_v2_delivery_and_still_rejects_a_bad_one(
    frozen: FakeClock,
) -> None:
    delivery: Dict[str, Any] = {
        "payload": RAW_BODY.encode(),
        "signature": v1(frozen),
        "signature_v2": v2(frozen),
        "timestamp": stamp(frozen),
        "delivery_id": DELIVERY_ID,
        "secret": SECRET,
        "require_v2": True,
    }
    assert verify_webhook_signature(**delivery)
    assert not verify_webhook_signature(**{**delivery, "delivery_id": "delivery-tampered"})
    assert not verify_webhook_signature(**{**delivery, "signature_v2": "0" * 64})


@pytest.mark.parametrize(
    "payload",
    [{}, None, {"event": "ping"}, [], 42, True, RAW_BODY, "text", json.loads(RAW_BODY)],
)
def test_a_payload_that_is_not_raw_bytes_raises_a_type_error_not_false(
    frozen: FakeClock, payload: Any
) -> None:
    with pytest.raises(TypeError) as caught:
        verify_webhook_signature(
            payload=payload,
            signature_v2=v2(frozen),
            timestamp=stamp(frozen),
            delivery_id=DELIVERY_ID,
            secret=SECRET,
            require_v2=True,
        )
    assert "raw request body" in str(caught.value)
    assert "request.get_data()" in str(caught.value)


def test_a_payload_type_error_does_not_depend_on_the_headers_being_valid() -> None:
    with pytest.raises(TypeError):
        verify_webhook_signature(payload={}, signature="nope", timestamp="nope", secret=SECRET)  # type: ignore[arg-type]


def test_a_button_delivery_verifies_with_the_per_ping_secret(frozen: FakeClock) -> None:
    # The bytes and headers PingRoom's Worker sends a webhook button's URL, signed
    # with the per-Ping secret (see tests/test_contracts.py for its derivation).
    secret = "81a29861609a0f98edae7253d871a336aa20ef1df78d1dac0da521d983b54623"
    payload = json.dumps(
        {
            "event": "notification.action",
            "notification_id": "9b2f5c1e-0000-4000-8000-000000000001",
            "action_id": "clean",
            "room": {"code": "AB12CD", "name": "Ops"},
            "actor": {"id": "0123456789abcdef0123456789abcdef", "display_name": "Sam"},
            "correlation_id": None,
            "body": {"host": "db-2"},
            "timestamp": "2026-11-11T10:00:00+00:00",
        }
    )
    ts = stamp(frozen)
    delivery = "6c5b1a2e-0000-4000-8000-00000000000a"
    sig = sign(secret, f"v2\n{ts}\n{delivery}\n{payload}".encode())
    kwargs: Dict[str, Any] = {
        "signature_v2": sig,
        "timestamp": ts,
        "delivery_id": delivery,
        "require_v2": True,
    }
    assert verify_webhook_signature(payload=payload.encode(), secret=secret, **kwargs)
    assert not verify_webhook_signature(payload=payload.encode(), secret="ff" * 32, **kwargs)
    assert not verify_webhook_signature(
        payload=payload.replace("db-2", "db-3").encode(), secret=secret, **kwargs
    )
    assert not verify_webhook_signature(
        payload=payload.encode(), secret=secret, **{**kwargs, "timestamp": str(int(ts) - 3600)}
    )


def test_verification_uses_a_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch, frozen: FakeClock
) -> None:
    calls: List[Any] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("pingroom.webhooks.hmac.compare_digest", spy)
    verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=v1(frozen), timestamp=stamp(frozen), secret=SECRET
    )
    assert len(calls) == 1


def test_the_real_clock_is_used_when_not_faked() -> None:
    import time

    ts = str(int(time.time()))
    sig = sign(SECRET, f"{ts}.{RAW_BODY}".encode())
    assert verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=sig, timestamp=ts, secret=SECRET
    )
    stale = str(int(time.time()) - 400)
    stale_sig = sign(SECRET, f"{stale}.{RAW_BODY}".encode())
    assert not verify_webhook_signature(
        payload=RAW_BODY.encode(), signature=stale_sig, timestamp=stale, secret=SECRET
    )


# --- sending to an incoming webhook ------------------------------------------

URL = "https://api.pingroom.io/api/webhooks/AB12/secret"


class Hook:
    """Runs ``send_incoming_webhook`` (sync or async) against a recording transport."""

    def __init__(
        self, harness: Harness, handler: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.harness = harness
        self.handler = handler
        self.requests: List[httpx.Request] = []

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def send(self, url: str, payload: Any, **kwargs: Any) -> Any:
        transport = httpx.MockTransport(self._record)
        if self.harness.is_async:
            return asyncio.run(
                send_incoming_webhook_async(url, payload, transport=transport, **kwargs)
            )
        return send_incoming_webhook(url, payload, transport=transport, **kwargs)

    @property
    def body(self) -> Any:
        return json.loads(self.requests[-1].content)


def hook(harness: Harness, body: Any = None, status: int = 200) -> Hook:
    return Hook(
        harness, lambda request: reply(body if body is not None else {"success": True}, status)
    )


def test_it_posts_json_and_returns_the_result(harness: Harness) -> None:
    h = hook(harness, {"success": True, "notification_id": "n9"}, 201)
    result = h.send(
        URL,
        {
            "message": "hi",
            "action": 2,
            "data": {"a": 1},
            "requires_ack": True,
            "ack_timeout_seconds": 120,
        },
        idempotency_key="k1",
    )
    assert result["notification_id"] == "n9"
    request = h.requests[0]
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["idempotency-key"] == "k1"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "pingroom-python/0.1.0"
    assert "authorization" not in request.headers
    assert h.body == {
        "message": "hi",
        "action": 2,
        "data": {"a": 1},
        "requires_ack": True,
        "ack_timeout_seconds": 120,
    }


def test_an_explicit_null_action_bypasses_a_stored_preset(harness: Harness) -> None:
    h = hook(harness, {"success": True, "action": None})
    payload = {"title": "Deployment", "message": "Version 2 is live", "action": None}
    h.send(URL, payload)
    assert h.body == {"title": "Deployment", "message": "Version 2 is live", "action": None}


def test_public_room_text_boundaries_are_accepted_and_the_ceiling_is_enforced(
    harness: Harness,
) -> None:
    h = hook(harness)
    message, title = "m" * 160, "t" * 40
    h.send(URL, {"message": message, "title": title})
    assert h.body == {"title": title, "message": message}
    for payload, pattern in (
        ({"message": "m" * 161}, "160"),
        ({"message": "ok", "title": "t" * 41}, "40"),
    ):
        with pytest.raises(PingRoomError) as caught:
            h.send(URL, payload)
        assert pattern in caught.value.message
    assert len(h.requests) == 1


def test_the_field_list_is_the_servers_and_every_field_is_forwarded(harness: Harness) -> None:
    assert list(INCOMING_WEBHOOK_FIELDS) == [
        "title",
        "message",
        "action",
        "emoji",
        "icon",
        "color",
        "sound",
        "data",
        "correlation_id",
        "reply_to",
        "tags",
        "is_urgent",
        "requires_ack",
        "ack_mode",
        "ack_timeout_seconds",
        "live_status",
        "metric",
    ]
    payload = {
        "title": "Build 512",
        "message": "green",
        "action": 3,
        "emoji": "\U0001f680",
        "icon": "rocket",
        "color": "#e33122",
        "sound": "ting",
        "data": {"commit": "abc123"},
        "correlation_id": "c-1",
        "reply_to": "r-1",
        "tags": ["deploy", "prod"],
        "is_urgent": True,
        "requires_ack": True,
        "ack_mode": "all",
        "ack_timeout_seconds": 300,
        "live_status": {"state": "running", "template": "status"},
    }
    metric_payload = {
        **payload,
        "metric": {
            "key": "mrr",
            "value": 12400,
            "label": "MRR",
            "format": "currency",
            "unit": "USD",
        },
    }
    del metric_payload["live_status"]
    # Every documented field must be exercised, or a dropped one slips past.
    assert sorted({*payload, *metric_payload}) == sorted(INCOMING_WEBHOOK_FIELDS)
    h = hook(harness)
    for body in (payload, metric_payload):
        h.send(URL, body)
        assert h.body == body


def test_replace_key_is_sent_as_replace_key(harness: Harness) -> None:
    h = hook(harness, {"success": True, "notification_id": "n1", "replaced": True})
    result = h.send(URL, {"message": "Build 41: green", "replace_key": "build:41"})
    assert result["replaced"] is True
    assert h.body == {"message": "Build 41: green", "replace_key": "build:41"}
    for bad in ("no spaces allowed", "", "x" * 65):
        with pytest.raises(PingRoomError) as caught:
            h.send(URL, {"message": "m", "replace_key": bad})
        assert caught.value.code == "invalid_request"
    assert len(h.requests) == 1


def test_keys_outside_the_field_list_are_not_sent(harness: Harness) -> None:
    h = hook(harness)
    h.send(URL, {"message": "hi", "replaceKey": "x", "unknown": 1})
    assert h.body == {"message": "hi"}


def test_the_idempotency_key_rules_for_a_webhook(harness: Harness) -> None:
    h = hook(harness)
    for key in ("deploy-✅", "k" * 256, "   "):
        with pytest.raises(PingRoomError) as caught:
            h.send(URL, {"message": "hi"}, idempotency_key=key)
        assert caught.value.code == "invalid_request"
        assert "idempotency_key" in caught.value.message
    assert h.requests == []
    h.send(URL, {"message": "hi"}, idempotency_key="build 42")
    assert h.requests[-1].headers["idempotency-key"] == "build 42"
    # An empty key still means "no key" here.
    h.send(URL, {"message": "hi"}, idempotency_key="")
    assert "idempotency-key" not in h.requests[-1].headers


def test_an_insecure_url_is_refused(harness: Harness) -> None:
    h = hook(harness)
    with pytest.raises(PingRoomError) as caught:
        h.send("http://evil.example.com/x", {"message": "x"})
    assert caught.value.code == "insecure_url"
    h.send("http://localhost:8000/hook", {"message": "x"})
    h.send("http://hooks.corp/x", {"message": "x"}, allow_insecure=True)


def test_the_action_slot_is_validated(harness: Harness) -> None:
    h = hook(harness)
    with pytest.raises(PingRoomError) as caught:
        h.send("https://api.pingroom.io/x", {"action": 17})
    assert caught.value.code == "invalid_request"


def test_a_failure_body_raises_with_retry_after_and_the_server_code(harness: Harness) -> None:
    h = hook(
        harness,
        {"success": False, "error": "cooldown_active", "message": "wait", "retry_after": 5},
        429,
    )
    with pytest.raises(PingRoomError) as caught:
        h.send("https://api.pingroom.io/x", {"message": "hi"})
    assert (caught.value.code, caught.value.retry_after, caught.value.status) == (
        "cooldown_active",
        5,
        429,
    )


def test_a_2xx_that_says_success_false_maps_to_the_server_error(harness: Harness) -> None:
    body = {
        "success": False,
        "error": "webhook_disabled",
        "message": "Webhook is currently disabled",
    }
    h = hook(harness, body, 200)
    with pytest.raises(PingRoomError) as caught:
        h.send("https://api.pingroom.io/x", {"message": "hi"})
    assert (caught.value.code, caught.value.status) == ("webhook_disabled", 200)


@pytest.mark.parametrize(
    "text,status",
    [
        ("<html>Please sign in to the hotspot</html>", 200),
        ("OK", 200),
        ("", 200),
        ("", 204),
        ("null", 200),
        ("[]", 200),
        ('{"notification_id":"n1"}', 200),
        ('{"success":"true"}', 200),
        ('{"success":1}', 200),
    ],
)
def test_a_2xx_that_is_not_a_json_object_with_success_true_is_invalid_response(
    harness: Harness, text: str, status: int
) -> None:
    h = Hook(harness, lambda request: httpx.Response(status, content=text.encode()))
    url = "https://api.pingroom.io/api/webhooks/AB12/secret-SYNTHETIC"
    with pytest.raises(PingRoomError) as caught:
        h.send(url, {"message": "hi"})
    assert (caught.value.code, caught.value.status) == ("invalid_response", 0)
    assert "secret-SYNTHETIC" not in caught.value.message


def test_every_success_shape_the_server_sends_comes_back_whole(harness: Harness) -> None:
    bodies = [
        {
            "success": True,
            "message": "Webhook triggered successfully",
            "notification_id": "n1",
            "room": {"id": "r1", "name": "Builds"},
            "webhook": {"id": "w1", "name": "CI", "emoji": None, "icon": None},
            "action": None,
            "notification_title": None,
            "notification_message": "hi",
            "action_state": None,
            "triggered_at": "2026-11-07T10:00:00+00:00",
        },
        {
            "success": True,
            "notification_id": "n2",
            "correlation_id": "deploy-1",
            "action_state": None,
            "live_status": {"state": "running"},
            "idempotent": True,
        },
        {"success": True, "unchanged": True, "metric": {"key": "mrr", "value": 1}},
    ]
    for body in bodies:
        for status in (200, 201):
            assert hook(harness, body, status).send(URL, {"message": "hi"}) == body


def test_buttons_ride_an_ordinary_ping_and_are_refused_on_a_stream(harness: Harness) -> None:
    hook_button = {
        "id": "clean",
        "label": "Clean up now",
        "type": "webhook",
        "url": "https://ops.example.com/cleanup",
        "body": {"host": "db-2"},
        "style": "danger",
    }
    h = hook(
        harness, {"success": True, "notification_id": "n1", "action_signing_secret": "cd" * 32}
    )
    url = "https://api.example.test/api/webhooks/AB12/secret"
    result = h.send(url, {"message": "Disk 91%", "data": {"actions": [hook_button]}})
    assert h.body["data"]["actions"] == [hook_button]
    assert result["action_signing_secret"] == "cd" * 32
    with pytest.raises(PingRoomError) as caught:
        h.send(
            url,
            {
                "data": {"actions": [hook_button]},
                "live_status": {"state": "running"},
                "correlation_id": "c",
            },
        )
    assert caught.value.code == "invalid_data"
    assert "live-progress stream cannot carry buttons" in caught.value.message
    with pytest.raises(PingRoomError) as caught:
        h.send(
            url,
            {"message": "x", "data": {"actions": [{**hook_button, "url": "https://10.0.0.5/"}]}},
        )
    assert "public hostname" in caught.value.message
    assert len(h.requests) == 1


def test_tags_are_validated_and_ride_with_a_stream(harness: Harness) -> None:
    h = hook(harness)
    h.send(URL, {"message": "green", "tags": ["ci", "main"]})
    assert h.body["tags"] == ["ci", "main"]
    h.send(URL, {"correlation_id": "c1", "tags": ["nightly"], "live_status": {"state": "running"}})
    assert h.body["tags"] == ["nightly"]
    with pytest.raises(PingRoomError):
        h.send(URL, {"message": "x", "tags": ["bad tag"]})
    assert len(h.requests) == 2


def test_a_metric_is_validated_like_metrics_set_and_refused_with_live_status(
    harness: Harness,
) -> None:
    h = hook(harness, {"success": True, "metric": {"key": "mrr"}})
    cases = [
        ({"metric": {"key": "bad key", "value": 1}}, "`metric.key` must be 1–40"),
        ({"metric": {"key": "mrr"}}, "`metric.value` is required"),
        ({"metric": {"key": "mrr", "value": float("nan")}}, "finite number"),
        (
            {"metric": {"key": "mrr", "value": 1, "format": "currency", "unit": "US"}},
            "`metric.unit` for the currency format",
        ),
        (
            {"metric": {"key": "mrr", "value": 1, "format": "number", "unit": "ms"}},
            "`metric.unit` is only allowed",
        ),
        (
            {"metric": {"key": "mrr", "value": "x" * 25, "format": "text"}},
            "`metric.value` must be 1–24",
        ),
        ({"metric": "mrr"}, "`metric` must be an object"),
        ({"metric": None}, "`metric` must be an object"),
        ({"metric": [1]}, "`metric` must be an object"),
        (
            {
                "metric": {"key": "mrr", "value": 1},
                "live_status": {"state": "running"},
                "correlation_id": "c",
            },
            "cannot be combined with `live_status`",
        ),
    ]
    for payload, expected in cases:
        with pytest.raises(PingRoomError) as caught:
            h.send(URL, payload)
        assert caught.value.code == "invalid_request"
        assert expected in caught.value.message, payload
    assert h.requests == []
    result = h.send(URL, {"metric": {"key": "MRR", "value": 12400}})
    assert result["metric"]["key"] == "mrr"
    assert h.body == {"metric": {"key": "MRR", "value": 12400}}


def test_a_metric_envelope_refusal_surfaces(harness: Harness) -> None:
    body = {
        "success": False,
        "error": "metrics_limit_reached",
        "code": "metrics_limit_reached",
        "message": "Maximum of 8 metrics per room reached.",
    }
    h = hook(harness, body, 422)
    with pytest.raises(PingRoomError) as caught:
        h.send(URL, {"metric": {"key": "ninth", "value": 1}})
    assert (caught.value.status, caught.value.code) == (422, "metrics_limit_reached")


def test_a_malformed_url_is_never_echoed(harness: Harness) -> None:
    h = hook(harness)
    with pytest.raises(PingRoomError) as caught:
        h.send("not-a-url/secret-SYNTHETIC", {"message": "hi"})
    assert caught.value.code == "invalid_url"
    assert "secret-SYNTHETIC" not in caught.value.message


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_never_replays_the_webhook_body(harness: Harness, status: int) -> None:
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "api.pingroom.io":
            return httpx.Response(status, headers={"Location": "https://evil.example.com/collect"})
        return httpx.Response(200, json={"success": True})

    h = Hook(harness, handler)
    with pytest.raises(PingRoomError) as caught:
        h.send(
            "https://api.pingroom.io/hook/fixture-secret", {"message": "fixture-private-message"}
        )
    assert (caught.value.code, caught.value.status) == ("redirect_refused", status)
    assert seen == ["api.pingroom.io"]


def test_a_wrapped_transport_error_loses_the_webhook_url(harness: Harness) -> None:
    url = "https://example.invalid/webhooks/secret-SYNTHETIC?x=1"

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"request to {url} failed, reason: getaddrinfo ENOTFOUND (path /webhooks/secret-SYNTHETIC)",
            request=request,
        )

    h = Hook(harness, failing)
    with pytest.raises(PingRoomNetworkError) as caught:
        h.send(url, {"message": "hi"})
    assert caught.value.code == "network_error"
    assert "secret-SYNTHETIC" not in caught.value.message
    assert "https://example.invalid/[redacted]" in caught.value.message
    assert "ENOTFOUND" in caught.value.message  # the useful part of the reason survives
    assert caught.value.message.startswith("Webhook request failed: ")


def test_a_webhook_timeout_is_a_timeout_error(harness: Harness) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    h = Hook(harness, slow)
    with pytest.raises(PingRoomTimeoutError):
        h.send(URL, {"message": "hi"}, timeout=0.1)
    with pytest.raises(PingRoomError) as caught:
        h.send(URL, {"message": "hi"}, timeout=0)
    assert caught.value.code == "invalid_request"


def test_the_webhook_user_agent_can_be_overridden(harness: Harness) -> None:
    h = hook(harness)
    h.send(URL, {"message": "hi"}, user_agent="ci-bot/1")
    assert h.requests[-1].headers["user-agent"] == "ci-bot/1"
