"""Webhook helpers. Usable on their own: no client or token is required.

* ``verify_webhook_signature``: verify an OUTGOING webhook PingRoom sent you,
  or the signed delivery of a Ping's ``webhook`` button (the same scheme, with
  the Ping's ``action_signing_secret`` as the secret). New deliveries carry both
  the legacy timestamp/body HMAC and a v2 HMAC that also binds the delivery id.
  Verify over the EXACT raw request body bytes: re-serialising the parsed JSON
  will not match.
* ``send_incoming_webhook``: fire a room's INCOMING webhook (the URL carries its
  own secret, so no agent token is needed): the one-line CI or deploy ping. Pass
  a top-level ``live_status`` to drive a Live Activity instead, or a top-level
  ``metric`` to update a room metric (no Ping is sent).
"""

from __future__ import annotations

import hmac
import math
import re
from hashlib import sha256
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import httpx

from . import _clock
from ._core import (
    Prepared,
    RawResponse,
    assert_secure_url,
    decode_text,
    encode_json,
    invalid_success_response,
    parse_json,
    refuse_redirect,
)
from ._driver import send_async, send_sync
from ._guards import (
    MAX_PING_TITLE_LENGTH,
    MAX_PUBLIC_PING_MESSAGE_LENGTH,
    assert_action_number,
    assert_max_length,
    assert_replace_key,
    assert_structured_data,
    assert_tags,
    invalid_request,
    normalize_idempotency_key,
)
from ._metrics import assert_metric_fields, normalize_metric_key
from ._ping_actions import assert_ping_actions_data, refuse_ping_actions_data
from ._text import ascii_lower, js_trim
from ._version import __version__
from .errors import PingRoomError

__all__ = [
    "INCOMING_WEBHOOK_FIELDS",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "WEBHOOK_SIGNATURE_V2_HEADER",
    "WEBHOOK_TIMESTAMP_HEADER",
    "send_incoming_webhook",
    "send_incoming_webhook_async",
    "verify_webhook_signature",
]

WEBHOOK_SIGNATURE_HEADER = "X-PingRoom-Signature"
WEBHOOK_SIGNATURE_V2_HEADER = "X-PingRoom-Signature-V2"
WEBHOOK_TIMESTAMP_HEADER = "X-PingRoom-Timestamp"
WEBHOOK_DELIVERY_HEADER = "X-PingRoom-Delivery"
WEBHOOK_EVENT_HEADER = "X-PingRoom-Event"

#: The default allowed timestamp skew, in seconds.
DEFAULT_MAX_AGE_SECONDS = 300

# ---------------------------------------------------------------------------
# Verifying a delivery
# ---------------------------------------------------------------------------

_SAFE_INTEGER_MAX = 2**53 - 1
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_DELIVERY_ID = re.compile(r"[\x21-\x7e]{1,255}")
_DIGITS = re.compile(r"[0-9]+")


def _raw_payload(payload: Any) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, memoryview):
        # The view's own bytes, never element by element.
        return payload.tobytes()
    if payload is None:
        received = "None"
    elif isinstance(payload, str):
        received = "a str"
    else:
        received = f"a {type(payload).__name__}"
    raise TypeError(
        "verify_webhook_signature: `payload` must be the raw request body as bytes "
        f"(bytes, bytearray or memoryview), got {received}. Capture the raw bytes before any "
        "JSON parsing or decoding (Flask: request.get_data(); Django: request.body; "
        "Starlette and FastAPI: await request.body())."
    )


def _normalize_timestamp(timestamp: Any) -> Optional[str]:
    if timestamp is None or isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, str):
        normalized = js_trim(timestamp)
    elif isinstance(timestamp, int):
        normalized = str(timestamp)
    elif isinstance(timestamp, float) and math.isfinite(timestamp) and timestamp.is_integer():
        normalized = str(int(timestamp))
    else:
        return None
    if not _DIGITS.fullmatch(normalized):
        return None
    return normalized if int(normalized) <= _SAFE_INTEGER_MAX else None


def _normalize_signature(signature: str) -> str:
    trimmed = js_trim(signature)
    return trimmed[len("sha256=") :] if trimmed.startswith("sha256=") else trimmed


def _normalize_delivery_id(delivery_id: Any) -> Optional[str]:
    if isinstance(delivery_id, str) and _DELIVERY_ID.fullmatch(delivery_id):
        return delivery_id
    return None


def verify_webhook_signature(
    *,
    payload: Union[bytes, bytearray, memoryview],
    secret: Union[str, bytes],
    timestamp: Union[str, int, None],
    signature: Optional[str] = None,
    signature_v2: Optional[str] = None,
    delivery_id: Optional[str] = None,
    require_v2: bool = False,
    max_age_seconds: Union[int, float] = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Constant-time verification of an outgoing-webhook signature.

    Returns a bool and never raises on a mismatch. Pass ``require_v2=True`` so a
    delivery without the v2 signature is refused rather than checked against
    the legacy v1 signature: v1 does not sign ``X-PingRoom-Delivery``, so a
    captured body replayed without its v2 header under a new delivery id still
    passes v1. Every current delivery carries v2, so set ``require_v2`` and
    always set it when you dedupe on the delivery id.

    Args:
        payload: The raw request body, the exact bytes received (``bytes``,
            ``bytearray`` or ``memoryview``), NOT a re-serialised object and not
            decoded text. Anything else (a ``dict`` or ``None`` from a body
            parser that did not capture the raw bytes, or a ``str``) raises
            ``TypeError`` rather than failing verification, so a receiver bug
            cannot look like a forged signature.
        secret: The webhook's ``signing_secret``.
        timestamp: The ``X-PingRoom-Timestamp`` header value (Unix seconds).
        signature: The legacy ``X-PingRoom-Signature`` value, with or without
            the ``sha256=`` prefix. Tried only when ``signature_v2`` is absent
            and ``require_v2`` is not set.
        signature_v2: The ``X-PingRoom-Signature-V2`` value. When present, v2
            MUST verify; v1 is not tried.
        delivery_id: The ``X-PingRoom-Delivery`` value, required by the v2
            signed message.
        require_v2: Refuse a delivery that carries no v2 signature.
        max_age_seconds: Allowed timestamp skew in seconds (default 300).
    """
    body = _raw_payload(payload)
    has_v2 = signature_v2 is not None
    if require_v2 and not has_v2:
        return False
    selected = signature_v2 if has_v2 else signature
    if not selected or not isinstance(selected, str) or not secret:
        return False
    normalized_timestamp = _normalize_timestamp(timestamp)
    if normalized_timestamp is None:
        return False
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(max_age_seconds)
        or max_age_seconds < 0
    ):
        return False
    if abs(_clock.now() - int(normalized_timestamp)) > max_age_seconds:
        return False
    provided = ascii_lower(_normalize_signature(selected))
    if not _HEX_64.fullmatch(provided):
        return False
    if has_v2:
        normalized_delivery = _normalize_delivery_id(delivery_id)
        if normalized_delivery is None:
            return False
        prefix = f"v2\n{normalized_timestamp}\n{normalized_delivery}\n".encode("utf-8")
    else:
        prefix = f"{normalized_timestamp}.".encode("utf-8")
    key = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    expected = hmac.new(key, prefix + body, sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Sending to an incoming webhook
# ---------------------------------------------------------------------------

#: Every field the incoming-webhook endpoint accepts, in the server's own order
#: (``WebhookController::trigger``'s validate block, plus ``live_status``, which
#: it detects before that block and routes to the stream handler).
#:
#: The request body is built by walking this list rather than a hand-written
#: run of ``if``s, which silently dropped whatever nobody remembered to add.
#: ``replace_key`` is also a server field; it is carried after this list.
INCOMING_WEBHOOK_FIELDS: Tuple[str, ...] = (
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
    # Top level, never folded into `data`: the server routes on this key and
    # strips it from `data` so a legacy-path caller cannot spoof completion alerts.
    "live_status",
    # Top level as well: a body with a `metric` updates a room metric and sends no Ping.
    "metric",
)

_DEFAULT_TIMEOUT = 30.0


def _assert_webhook_metric(payload: Mapping[str, Any]) -> None:
    """A ``metric`` body is validated the way ``metrics.set()`` validates its
    arguments, and cannot ride with ``live_status``: the server answers 422
    rather than pick one."""
    if "metric" not in payload:
        return
    if "live_status" in payload:
        raise invalid_request(
            "`metric` cannot be combined with `live_status`; send them as separate requests."
        )
    metric = payload["metric"]
    if not isinstance(metric, dict):
        raise invalid_request("`metric` must be an object with a `key` and a `value`.")
    normalize_metric_key(metric.get("key"), "metric.key")
    assert_metric_fields(metric, require_value=True, prefix="metric.")


def _prepare_incoming(
    webhook_url: str,
    payload: Mapping[str, Any],
    idempotency_key: Optional[str],
    timeout: float,
    allow_insecure: bool,
    user_agent: Optional[str],
) -> Prepared:
    assert_secure_url(webhook_url, allow_insecure)
    if not isinstance(payload, Mapping):
        raise invalid_request("`payload` must be a mapping of incoming-webhook fields.")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise invalid_request("`timeout` must be a positive number of seconds.")
    # A webhook URL does not reveal room visibility, so validate the public
    # ceiling here and let the server enforce 120 for private rooms.
    assert_max_length(payload.get("message"), MAX_PUBLIC_PING_MESSAGE_LENGTH, "message")
    assert_max_length(payload.get("title"), MAX_PING_TITLE_LENGTH, "title")
    assert_action_number(payload.get("action"))
    assert_structured_data(payload.get("data"))
    # Buttons ride an ordinary webhook Ping; a `live_status` body is a stream,
    # whose Ping never carries them (the server answers 422).
    if "live_status" in payload:
        refuse_ping_actions_data(payload.get("data"), "A live-progress stream")
    else:
        assert_ping_actions_data(payload.get("data"))
    assert_tags(payload.get("tags"))
    assert_replace_key(payload.get("replace_key"))
    _assert_webhook_metric(payload)

    body: Dict[str, Any] = {}
    for name in INCOMING_WEBHOOK_FIELDS:
        if name in payload:
            body[name] = payload[name]
    if payload.get("replace_key") is not None:
        body["replace_key"] = payload["replace_key"]

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": user_agent or f"pingroom-python/{__version__}",
    }
    # An empty key means "no key" here, as it always has.
    if idempotency_key:
        headers["Idempotency-Key"] = normalize_idempotency_key(idempotency_key)
    return Prepared(
        method="POST",
        url=webhook_url,
        headers=headers,
        content=encode_json(body),
        files=None,
        timeout=float(timeout),
        mode="json",
    )


def _interpret_incoming(raw: RawResponse) -> Dict[str, Any]:
    # A 307/308 must not replay this body to another origin.
    refuse_redirect(raw.status)
    text = decode_text(raw.body)
    parsed: Any = None
    if text:
        try:
            parsed = parse_json(text)
        except ValueError:
            parsed = text
    ok = 200 <= raw.status < 300
    if not ok or (isinstance(parsed, dict) and parsed.get("success") is False):
        raise PingRoomError.from_response(raw.status, parsed, raw.retry_after)
    # Every success the endpoint sends is a JSON object with `success: true`.
    # Anything else is a misrouted URL or a proxy's page, not a delivered Ping.
    if not isinstance(parsed, dict) or parsed.get("success") is not True:
        raise invalid_success_response(raw.status, "a JSON object with `success: true`", parsed)
    return parsed


def send_incoming_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    idempotency_key: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: Optional[httpx.BaseTransport] = None,
    allow_insecure: bool = False,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """POST to a room's incoming-webhook URL (which embeds the secret in its path).

    ``payload`` maps the endpoint's field names to values. Only the fields in
    ``INCOMING_WEBHOOK_FIELDS`` and ``replace_key`` are sent; any other key is
    ignored. A key that is present with the value ``None`` is sent as JSON
    ``null``: ``{"action": None}`` bypasses the webhook's stored preset.

    ``idempotency_key`` is sent as the ``Idempotency-Key`` header; identical
    keys within about an hour replay the first result, so retrying a send with
    the same key is safe. Omitted or empty keys send no header. Other keys must
    be 1-255 printable ASCII characters, else ``invalid_request`` before sending.

    No redirect is followed. Errors never echo the URL, which holds the secret.
    """
    prepared = _prepare_incoming(
        webhook_url, payload, idempotency_key, timeout, allow_insecure, user_agent
    )
    with httpx.Client(transport=transport) as client:
        raw = send_sync(
            client, prepared, redact_url=webhook_url, error_prefix="Webhook request failed"
        )
    return _interpret_incoming(raw)


async def send_incoming_webhook_async(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    idempotency_key: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    allow_insecure: bool = False,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """The asynchronous ``send_incoming_webhook``."""
    prepared = _prepare_incoming(
        webhook_url, payload, idempotency_key, timeout, allow_insecure, user_agent
    )
    async with httpx.AsyncClient(transport=transport) as client:
        raw = await send_async(
            client, prepared, redact_url=webhook_url, error_prefix="Webhook request failed"
        )
    return _interpret_incoming(raw)
