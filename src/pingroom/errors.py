"""Error types and the API's error-code vocabulary.

``PingRoomError`` carries the HTTP status and the API's machine ``code`` (for
example ``room_not_granted``, ``cooldown_active`` or ``rate_limited``), so
callers can branch without matching message text. Retry any 429 after
``retry_after`` rather than matching one code: route throttles answer
``rate_limited``, but public-room and quick-action limits carry their own
(``public_room_rate_limited``, ``cooldown_active``).

The agent token is never stored on, or serialised through, an error.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar

from ._text import js_trim

__all__ = [
    "AGENT_INBOX_ERROR_CODES",
    "COMMON_ERROR_CODES",
    "HANDOFF_ERROR_CODES",
    "LIVE_CLAIM_ERROR_CODES",
    "METRIC_ERROR_CODES",
    "PING_ACTION_ERROR_CODES",
    "QUICK_ACTION_ERROR_CODES",
    "REPLACE_ERROR_CODES",
    "REQUEST_LIMIT_ERROR_CODES",
    "ROOM_SCOPED_ERROR_CODES",
    "WITHDRAW_ERROR_CODES",
    "PingRoomCancelledError",
    "PingRoomError",
    "PingRoomNetworkError",
    "PingRoomTimeoutError",
]

# ---------------------------------------------------------------------------
# Error-code vocabulary. One tuple per surface, in the server's order. These
# are pinned to the TypeScript SDK's lists by tests/test_errors.py.
# ---------------------------------------------------------------------------

#: The code the ``agent.room`` gate emits on every room-scoped route (HTTP 403):
#: the room is outside the grant the human gave this agent when connecting.
ROOM_SCOPED_ERROR_CODES: Tuple[str, ...] = ("room_not_granted",)

#: Codes specific to creating or reading a handoff.
HANDOFF_ERROR_CODES: Tuple[str, ...] = (
    "feature_temporarily_unavailable",
    "handoff_room_unsupported",
    "invalid_target",
    "no_room_configured",
    "target_not_permitted",
    "recipient_not_ready",
    "idempotency_conflict",
    "invalid_idempotency_key",
    "capability_check_unavailable",
    "insufficient_scope",
    "free_limit_reached",
)

#: The coded failures of the Agent Inbox activation path (not wrapped by this SDK yet).
AGENT_INBOX_ERROR_CODES: Tuple[str, ...] = (
    "activation_evidence_unavailable",
    "no_room_configured",
    "activation_room_muted",
    "activation_delivery_unavailable",
    "feature_temporarily_unavailable",
)

#: The coded failures of the quick-action surface (not wrapped by this SDK yet).
QUICK_ACTION_ERROR_CODES: Tuple[str, ...] = (
    "quick_action_input_required",
    "quick_action_input_type",
    "action_not_configured",
    "pro_required",
    "quick_action_layout_changed",
    "quick_action_page_in_use",
    "location_trigger_owner_only",
    "location_trigger_inactive",
)

#: The coded refusals of the "Who's on it?" surface (``live.claim()`` and a
#: ``live.push()`` that starts a ``claim`` stream).
LIVE_CLAIM_ERROR_CODES: Tuple[str, ...] = (
    "claim_conflict",
    "claim_own_request",
    "claim_private_rooms_only",
    "claim_not_found",
    "claim_request_invalid",
    "claim_must_start_running",
    "claim_deadline_out_of_range",
)

#: The coded refusals of ``notifications.replace()``. A bare 404 with no
#: ``code`` means the server has replacing a Ping switched off.
REPLACE_ERROR_CODES: Tuple[str, ...] = (
    "notification_not_found",
    "not_ping_owner",
    "unsupported_room",
    "unsupported_question",
    "unsupported_handoff",
    "unsupported_live_status",
    "unsupported_claim",
    "unsupported_ack",
    "unsupported_checklist",
    "unsupported_source",
    "unsupported_actions",
    "replace_window_closed",
    "replace_limit_reached",
    "ping_withdrawn",
)

#: The coded refusals of ``notifications.withdraw()``. A bare 404 with no
#: ``code`` means the server has withdrawing a Ping switched off.
WITHDRAW_ERROR_CODES: Tuple[str, ...] = (
    "notification_not_found",
    "not_ping_owner",
    "unsupported_room",
    "unsupported_question",
    "unsupported_handoff",
    "unsupported_live_status",
    "unsupported_claim",
    "unsupported_ack",
    "unsupported_checklist",
    "unsupported_source",
    "withdraw_window_closed",
)

#: The coded refusals of room metrics (``metrics.set()``, ``metrics.delete()``
#: and an incoming webhook's ``metric``).
METRIC_ERROR_CODES: Tuple[str, ...] = (
    "metrics_limit_reached",
    "metric_not_found",
    "metric_exists",
)

#: Codes any route can answer: ``rate_limited`` (429, carries ``retry_after``)
#: and ``feature_disabled`` (403, the operator switched the capability off;
#: ``error.body["feature"]`` names the switch, and retrying cannot succeed).
COMMON_ERROR_CODES: Tuple[str, ...] = ("rate_limited", "feature_disabled")

#: Request-size failures: the server's 413 and the SDK's local 5 MiB check.
REQUEST_LIMIT_ERROR_CODES: Tuple[str, ...] = ("payload_too_large", "attachment_too_large")

#: The coded refusals of a send that carries buttons (``data.actions``).
PING_ACTION_ERROR_CODES: Tuple[str, ...] = (
    "ping_buttons_agents_only",
    "ping_buttons_unsupported_room",
    "ping_buttons_with_ack",
    "ping_buttons_require_pro",
)


_E = TypeVar("_E", bound="PingRoomError")


class PingRoomError(Exception):
    """A failed PingRoom call, or a request the SDK refused to send.

    Attributes:
        status: HTTP status, or ``0`` for client-side and network errors.
        code: Machine-readable code from the API body (``code`` or ``error``),
            or a client code such as ``invalid_request``, ``network_error``,
            ``timeout``, ``redirect_refused`` or ``invalid_response``.
        retry_after: Seconds to wait before retrying, when the API body or a
            ``Retry-After`` header gives one.
        body: The parsed response body (or raw text), for inspection. It never
            holds a credential.
    """

    status: int
    code: Optional[str]
    retry_after: Optional[float]
    body: Any

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.body = body

    @classmethod
    def from_response(
        cls,
        status: int,
        body: Any,
        retry_after_header: Optional[str] = None,
    ) -> "PingRoomError":
        """Map an error response to an exception.

        ``body`` is the parsed JSON (or the raw text when it was not JSON).
        ``retry_after_header`` is the ``Retry-After`` header value, used only
        when the body carries no ``retry_after`` of its own.
        """
        fields: Mapping[str, Any] = body if isinstance(body, dict) else {}
        raw_code = fields.get("code")
        raw_error = fields.get("error")
        code: Optional[str] = None
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        elif isinstance(raw_error, str) and raw_error:
            code = raw_error
        raw_message = fields.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        elif code:
            message = code
        else:
            message = f"HTTP {status}"

        retry_after: Optional[float] = None
        raw_retry = fields.get("retry_after")
        if isinstance(raw_retry, (int, float)) and not isinstance(raw_retry, bool):
            retry_after = raw_retry
        elif retry_after_header is not None:
            trimmed = js_trim(retry_after_header)
            if re.fullmatch(r"[0-9]+", trimmed):
                retry_after = int(trimmed)
        return cls(message, status=status, code=code, retry_after=retry_after, body=body)

    def __reduce__(self) -> Tuple[Any, ...]:
        # Exceptions with keyword-only state do not survive pickle by default.
        return (_restore_error, (type(self), self.args, dict(self.__dict__)))


def _restore_error(cls: Type[_E], args: Tuple[Any, ...], state: Dict[str, Any]) -> _E:
    error = cls.__new__(cls)
    error.args = args
    error.__dict__.update(state)
    return error


class PingRoomTimeoutError(PingRoomError):
    """A request exceeded its timeout (``code == "timeout"``)."""

    def __init__(self, message: str = "Request timed out") -> None:
        super().__init__(message, code="timeout")


class PingRoomNetworkError(PingRoomError):
    """The transport failed: DNS, connection, TLS and the like (``code == "network_error"``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="network_error")


class PingRoomCancelledError(PingRoomError):
    """The caller's cancel signal was set while the SDK was waiting (``code == "cancelled"``).

    ``notifications.listen()`` ends quietly instead of raising it.
    """

    def __init__(self, message: str = "The wait was cancelled") -> None:
        super().__init__(message, code="cancelled")
