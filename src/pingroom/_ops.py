"""Every API operation, written once.

Each function validates its input, then yields the HTTP request it wants sent
and returns what the API answers. A driver (``_driver``) performs the I/O, so
the blocking ``PingRoom`` and the ``AsyncPingRoom`` call exactly this code.
Validation errors raise on the first step, before any request.

Each operation sends what the TypeScript SDK sends for the same input: same
method, path, query and JSON body keys, same headers.
"""

from __future__ import annotations

import math
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    cast,
)

from . import _clock
from ._core import UNSET, Command, Emit, Multipart, Request, Sleep, encode_path_segment
from ._guards import (
    MAX_PING_TITLE_LENGTH,
    MAX_PUBLIC_PING_MESSAGE_LENGTH,
    assert_action_number,
    assert_max_length,
    assert_replace_key,
    assert_structured_data,
    assert_tag,
    assert_tags,
    invalid_request,
    is_number,
    normalize_idempotency_key,
    require_non_empty,
)
from ._metrics import assert_metric_fields, normalize_metric_key
from ._ping_actions import assert_ping_actions_data, refuse_ping_actions_data
from ._retry import (
    MIN_HANDOFF_POLL_INTERVAL,
    MIN_LISTEN_POLL_INTERVAL,
    MIN_WAIT_POLL_INTERVAL,
    is_cancelled,
    is_transient_error,
    poll_until_settled,
    transient_delay,
    wait_timeout,
)
from ._text import js_trim
from .errors import PingRoomError

T = TypeVar("T")
Op = Generator[Command, Any, T]

#: Server cap per uploaded file; a larger body is a 413 the server answers before reading it.
ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024

_Query = Tuple[Tuple[str, str], ...]


def _enc(segment: str) -> str:
    return encode_path_segment(segment)


def _drop_none(fields: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _query(**fields: Any) -> _Query:
    """The query string pairs for the fields that are set, in the order given."""
    return tuple((key, str(value)) for key, value in fields.items() if value is not None)


def _idempotency_headers(key: Optional[str]) -> Dict[str, str]:
    """The ``Idempotency-Key`` header the server reads. An absent key sends none."""
    if key is None:
        return {}
    return {"Idempotency-Key": normalize_idempotency_key(key)}


def _assert_on_retry(on_retry: Any) -> None:
    if on_retry is not None and not callable(on_retry):
        raise invalid_request("`on_retry` must be callable.")


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and js_trim(value) != ""


def _same_id(received: Any, expected: str) -> bool:
    return isinstance(received, str) and received.lower() == expected.lower()


def _invalid_wait_response(
    subject: str, expected_id: str, value: Any, state_field: str
) -> PingRoomError:
    record = value if isinstance(value, dict) else {}
    received_id = record.get("id")
    state = record.get(state_field)
    return PingRoomError(
        f"PingRoom returned an invalid {subject} wait response.",
        code="invalid_response",
        body={
            "expected_id": expected_id,
            "received_id": received_id if isinstance(received_id, str) else None,
            state_field: state if isinstance(state, str) else None,
        },
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def broadcast(
    invite_code: str,
    message: str,
    *,
    title: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    correlation_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    action_number: Optional[int] = None,
    action_icon: Optional[str] = None,
    attachment_ids: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_mode: Optional[str] = None,
    is_urgent: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
    replace_key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Op[Any]:
    require_non_empty(message, "message")
    # The invite code does not expose room visibility. Permit the public-room
    # ceiling locally; Laravel applies 120 for private and 160 for public rooms.
    assert_max_length(message, MAX_PUBLIC_PING_MESSAGE_LENGTH, "message")
    assert_max_length(title, MAX_PING_TITLE_LENGTH, "title")
    number = assert_action_number(action_number)
    assert_structured_data(data)
    assert_ping_actions_data(data)
    assert_tags(tags)
    assert_replace_key(replace_key)
    headers = _idempotency_headers(idempotency_key)
    body = _drop_none(
        {
            "message": message,
            "title": title,
            "action_number": number,
            "action_icon": action_icon,
            "data": data,
            "correlation_id": correlation_id,
            "reply_to": reply_to,
            "tags": list(tags) if tags is not None else None,
            "attachment_ids": list(attachment_ids) if attachment_ids is not None else None,
            "requires_ack": requires_ack,
            "ack_mode": ack_mode,
            "is_urgent": is_urgent,
            "ack_timeout_seconds": ack_timeout_seconds,
            "replace_key": replace_key,
        }
    )
    result = yield Request(
        "POST", f"/api/agent/rooms/{_enc(invite_code)}/notifications", body=body, headers=headers
    )
    return result


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def notifications_list(
    *,
    type: Optional[str] = None,
    room_id: Optional[str] = None,
    tag: Optional[str] = None,
    date: Optional[str] = None,
    limit: Optional[int] = None,
    page: Optional[int] = None,
    notifications_per_page: Optional[int] = None,
) -> Op[Any]:
    assert_tag(tag)
    effective_limit = limit if limit is not None else notifications_per_page
    result = yield Request(
        "GET",
        "/api/agent/notifications",
        query=_query(
            type=type, room_id=room_id, tag=tag, date=date, limit=effective_limit, page=page
        ),
    )
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result
    rows: List[Any] = []
    for row in result["data"]:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        room = row.get("room")
        item = {key: value for key, value in row.items() if key != "room"}
        if room:
            # History keeps the API's room object and adds the listener-compatible `code`.
            item["room"] = (
                {**room, "code": room.get("invite_code")} if isinstance(room, dict) else room
            )
        rows.append(item)
    return {**result, "data": rows}


def notifications_get(notification_id: str) -> Op[Any]:
    require_non_empty(notification_id, "notification_id")
    result = yield Request("GET", f"/api/agent/notifications/{_enc(notification_id)}")
    return result


def notifications_replace(
    invite_code: str,
    notification_id: str,
    message: str,
    *,
    title: Any = UNSET,
    data: Any = UNSET,
) -> Op[Any]:
    """``title`` and ``data`` are left out of the request when they are ``UNSET``
    (the server keeps them) and sent as given otherwise, ``None`` included (it clears them)."""
    require_non_empty(invite_code, "invite_code")
    require_non_empty(notification_id, "notification_id")
    require_non_empty(message, "message")
    assert_max_length(message, MAX_PUBLIC_PING_MESSAGE_LENGTH, "message")
    title_value = None if title is UNSET else title
    data_value = None if data is UNSET else data
    assert_max_length(title_value, MAX_PING_TITLE_LENGTH, "title")
    assert_structured_data(data_value)
    # A replacement never adds, changes or removes buttons (the server answers
    # 422 on `data.actions`), and a Ping that has buttons cannot be replaced.
    refuse_ping_actions_data(data_value, "A replacement")
    body: Dict[str, Any] = {"message": message}
    if title is not UNSET:
        body["title"] = title
    if data is not UNSET:
        body["data"] = data
    result = yield Request(
        "PUT",
        f"/api/agent/rooms/{_enc(invite_code)}/notifications/{_enc(notification_id)}",
        body=body,
    )
    return result


def notifications_withdraw(invite_code: str, notification_id: str) -> Op[Any]:
    require_non_empty(invite_code, "invite_code")
    require_non_empty(notification_id, "notification_id")
    result = yield Request(
        "POST",
        f"/api/agent/rooms/{_enc(invite_code)}/notifications/{_enc(notification_id)}/withdraw",
        body={},
    )
    return result


def notifications_wait_for_acknowledgement(
    notification_id: str, *, timeout: Optional[int] = None, cancel: Any = None
) -> Op[Any]:
    require_non_empty(notification_id, "notification_id")
    result = yield Request(
        "GET",
        f"/api/agent/notifications/{_enc(notification_id)}/ack/wait",
        query=_query(timeout=timeout),
        timeout=wait_timeout(timeout),
        cancel=cancel,
    )
    return result


def notifications_wait(
    *,
    after: Optional[str] = None,
    timeout: Optional[int] = None,
    limit: Optional[int] = None,
    tag: Optional[str] = None,
    cancel: Any = None,
) -> Op[Any]:
    assert_tag(tag)
    result = yield Request(
        "GET",
        "/api/agent/notifications/wait",
        query=_query(after=after, timeout=timeout, limit=limit, tag=tag),
        timeout=wait_timeout(timeout),
        cancel=cancel,
    )
    return result


def _validate_listen_result(value: Any, require_cursor: bool) -> Dict[str, Any]:
    """A 2xx that is not a wait answer (a captive portal's HTML, a proxy's error
    page served as 200) must not end ``listen()`` with a bare error, nor advance
    its cursor."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("notifications"), list)
        or any(not isinstance(item, dict) for item in value["notifications"])
        or (require_cursor if "cursor" not in value else not isinstance(value["cursor"], str))
    ):
        raise PingRoomError(
            "PingRoom returned an invalid notifications wait response.", code="invalid_response"
        )
    return value


def notifications_listen(
    *,
    after: Optional[str] = None,
    timeout: Optional[int] = None,
    limit: Optional[int] = None,
    tag: Optional[str] = None,
    cancel: Any = None,
    on_error: Optional[Callable[[PingRoomError], None]] = None,
    backoff: float = 1.0,
) -> Op[None]:
    """Continuous real-time inbound: long-poll in a loop, advancing the cursor.

    Emits each new ping. Ends when ``cancel`` is set. A transient failure is
    retried with backoff; any other error ends the loop by raising.
    """
    assert_tag(tag)
    cursor = after
    head_read = cursor is not None
    failures = 0
    while not is_cancelled(cancel):
        # The head read is an immediate answer by design; only polls ahead of
        # the cursor are spaced.
        polled_forward = head_read
        started = _clock.monotonic()
        try:
            if head_read:
                answer = yield from notifications_wait(
                    after=cursor, timeout=timeout, limit=limit, tag=tag, cancel=cancel
                )
            else:
                # No cursor yet: read the current head (an immediate, empty
                # answer) so only pings that land after this call are delivered.
                answer = yield from notifications_wait(timeout=0, cancel=cancel)
            # The head read exists to obtain a cursor, so it must carry one.
            result = _validate_listen_result(answer, require_cursor=not head_read)
        except Exception as error:
            if is_cancelled(cancel):
                return
            # A revoked credential or a bad request never heals; only network
            # failures, deadlines, 429 and 5xx are retried.
            if not isinstance(error, PingRoomError) or not is_transient_error(error):
                raise
            failures += 1
            if on_error is not None:
                on_error(error)
            yield Sleep(
                max(
                    transient_delay(error, failures, backoff),
                    MIN_LISTEN_POLL_INTERVAL - (_clock.monotonic() - started),
                ),
                cancel,
            )
            continue
        failures = 0
        head_read = True
        new_cursor = result.get("cursor")
        cursor = new_cursor if new_cursor is not None else cursor
        for notification in result["notifications"]:
            yield Emit(notification)
        # Yield between polls so cancellation can run. After an empty answer
        # also keep polls a floor apart, so `timeout=0` or an early-answering
        # proxy cannot spin the loop.
        spacing = (
            max(0.0, MIN_LISTEN_POLL_INTERVAL - (_clock.monotonic() - started))
            if polled_forward and len(result["notifications"]) == 0
            else 0.0
        )
        yield Sleep(spacing, cancel)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

_APPROVAL_STATUSES = frozenset({"pending", "decided", "expired", "cancelled"})


def _validate_approval_wait(value: Any, approval_id: str) -> Dict[str, Any]:
    """A wait response is trusted as an outcome only when it is the approval that
    was asked about, in a known status. A captive portal's HTML, ``{}``, or
    another approval must not read as "decided"."""
    status = value.get("status") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not _same_id(value.get("id"), approval_id)
        or not isinstance(status, str)
        or status not in _APPROVAL_STATUSES
        or "decided_at" not in value
        or not (value["decided_at"] is None or isinstance(value["decided_at"], str))
        # A gate acts on `decision`; a decided approval without one, or a
        # pending one already stamped decided, is not an outcome it can act on.
        or (status == "decided" and not _is_non_empty_string(value.get("decision")))
        or (status == "pending" and value["decided_at"] is not None)
    ):
        raise _invalid_wait_response("approval", approval_id, value, "status")
    return value


def approvals_request(
    invite_code: str,
    question: str,
    *,
    title: Optional[str] = None,
    options: Optional[Sequence[str]] = None,
    correlation_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    ttl: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> Op[Any]:
    require_non_empty(question, "question")
    assert_structured_data(data)
    refuse_ping_actions_data(data, "An approval")
    headers = _idempotency_headers(idempotency_key)
    body = _drop_none(
        {
            "question": question,
            "title": title,
            "options": list(options) if options is not None else None,
            "correlation_id": correlation_id,
            "data": data,
            "ttl": ttl,
        }
    )
    result = yield Request(
        "POST", f"/api/agent/rooms/{_enc(invite_code)}/approvals", body=body, headers=headers
    )
    return result


def approvals_get(approval_id: str) -> Op[Any]:
    result = yield Request("GET", f"/api/agent/approvals/{_enc(approval_id)}")
    return result


def approvals_wait(
    approval_id: str, *, timeout: Optional[int] = None, cancel: Any = None
) -> Op[Any]:
    result = yield Request(
        "GET",
        f"/api/agent/approvals/{_enc(approval_id)}/wait",
        query=_query(timeout=timeout),
        timeout=wait_timeout(timeout),
        cancel=cancel,
    )
    return result


def approvals_wait_for_decision(
    approval_id: str,
    *,
    cancel: Any = None,
    on_retry: Optional[Callable[[PingRoomError, int], None]] = None,
) -> Op[Any]:
    require_non_empty(approval_id, "approval_id")
    _assert_on_retry(on_retry)
    result = yield from poll_until_settled(
        poll=lambda: approvals_wait(approval_id, cancel=cancel),
        validate=lambda value: _validate_approval_wait(value, approval_id),
        settled=lambda approval: approval["status"] != "pending",
        expires_at=lambda approval: approval.get("expires_at"),
        what=f"approval {approval_id}",
        min_interval=MIN_WAIT_POLL_INTERVAL,
        cancel=cancel,
        on_retry=on_retry,
    )
    return result


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

_QUESTION_STATES = frozenset({"pending", "answered", "expired", "cancelled"})


def _validate_question_wait(value: Any, question_id: str) -> Dict[str, Any]:
    state = value.get("state") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not _same_id(value.get("id"), question_id)
        or not isinstance(state, str)
        or state not in _QUESTION_STATES
    ):
        raise _invalid_wait_response("question", question_id, value, "state")
    return value


def _assert_poll_shape(
    resolution_policy: Optional[str],
    responder_scope: Optional[str],
    target_user_id: Optional[str],
    text_input: Optional[Mapping[str, Any]],
) -> None:
    """A poll asks the whole room and counts option votes, so the server refuses
    it (422 on ``resolution_policy``) alongside a direct scope, a named target,
    or a typed answer. Fail before the request instead."""
    if resolution_policy != "all":
        return
    if (
        responder_scope == "direct"
        or (target_user_id is not None and target_user_id != "")
        or text_input is not None
    ):
        raise invalid_request(
            "A poll (`resolution_policy='all'`) asks the whole room with options only; it cannot "
            "use `responder_scope='direct'`, `target_user_id`, or `text_input`."
        )


def questions_ask(
    invite_code: str,
    prompt: str,
    *,
    context: Optional[str] = None,
    options: Optional[Sequence[Any]] = None,
    text_input: Optional[Dict[str, Any]] = None,
    responder_scope: Optional[str] = None,
    resolution_policy: Optional[str] = None,
    target_user_id: Optional[str] = None,
    ttl: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachment_ids: Optional[Sequence[str]] = None,
    idempotency_key: Optional[str] = None,
) -> Op[Any]:
    require_non_empty(prompt, "prompt")
    assert_structured_data(data)
    refuse_ping_actions_data(data, "A Question")
    _assert_poll_shape(resolution_policy, responder_scope, target_user_id, text_input)
    headers = _idempotency_headers(idempotency_key)
    body = _drop_none(
        {
            "prompt": prompt,
            "context": context,
            "options": list(options) if options is not None else None,
            "text_input": text_input,
            "responder_scope": responder_scope,
            "resolution_policy": resolution_policy,
            "target_user_id": target_user_id,
            "ttl": ttl,
            "data": data,
            "correlation_id": correlation_id,
            "reply_to": reply_to,
            "attachment_ids": list(attachment_ids) if attachment_ids is not None else None,
        }
    )
    result = yield Request(
        "POST", f"/api/agent/rooms/{_enc(invite_code)}/questions", body=body, headers=headers
    )
    return result


def questions_get(question_id: str) -> Op[Any]:
    result = yield Request("GET", f"/api/agent/questions/{_enc(question_id)}")
    return result


def questions_wait(
    question_id: str, *, timeout: Optional[int] = None, cancel: Any = None
) -> Op[Any]:
    result = yield Request(
        "GET",
        f"/api/agent/questions/{_enc(question_id)}/wait",
        query=_query(timeout=timeout),
        timeout=wait_timeout(timeout),
        cancel=cancel,
    )
    return result


def questions_wait_for_answer(
    question_id: str,
    *,
    cancel: Any = None,
    on_retry: Optional[Callable[[PingRoomError, int], None]] = None,
) -> Op[Any]:
    require_non_empty(question_id, "question_id")
    _assert_on_retry(on_retry)
    result = yield from poll_until_settled(
        poll=lambda: questions_wait(question_id, cancel=cancel),
        validate=lambda value: _validate_question_wait(value, question_id),
        settled=lambda question: question["state"] != "pending",
        expires_at=lambda question: question.get("expires_at"),
        what=f"question {question_id}",
        min_interval=MIN_WAIT_POLL_INTERVAL,
        cancel=cancel,
        on_retry=on_retry,
    )
    return result


def questions_list(*, state: Optional[str] = None) -> Op[Any]:
    result = yield Request("GET", "/api/agent/questions", query=_query(state=state))
    return _unwrap_list(result, "questions")


def questions_cancel(question_id: str) -> Op[Any]:
    result = yield Request("POST", f"/api/agent/questions/{_enc(question_id)}/cancel", body={})
    return result


def _unwrap_list(result: Any, key: str) -> List[Any]:
    if isinstance(result, dict):
        items = result.get(key)
        if isinstance(items, list):
            return items
    return []


# ---------------------------------------------------------------------------
# Handoffs
# ---------------------------------------------------------------------------

_HANDOFF_STATES = frozenset({"open", "acked", "pending", "answered", "expired", "cancelled"})
_HANDOFF_TERMINAL = frozenset({"acked", "answered", "expired", "cancelled"})


def _validate_handoff_wait(value: Any, handoff_id: str) -> Dict[str, Any]:
    state = value.get("state") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or not _same_id(value.get("id"), handoff_id)
        or not isinstance(state, str)
        or state not in _HANDOFF_STATES
    ):
        raise _invalid_wait_response("handoff", handoff_id, value, "state")
    return value


def _normalize_handoff_options(options: Sequence[Any]) -> List[Any]:
    """A bare string option is shorthand for ``{"value": s, "label": s}``."""
    return [{"value": o, "label": o} if isinstance(o, str) else o for o in options]


def _handoff_create(
    kind: str,
    prompt: str,
    *,
    target: Optional[str],
    expires_in: Optional[int],
    urgency: Optional[str],
    correlation_id: Optional[str],
    reply_to: Optional[str],
    data: Optional[Dict[str, Any]],
    idempotency_key: Optional[str],
    options: Optional[List[Any]] = None,
) -> Op[Any]:
    body = _drop_none(
        {
            "kind": kind,
            "prompt": prompt,
            "audience": {"type": "direct", "user_id": target if target is not None else "me"},
            "expires_in": expires_in,
            "urgency": urgency,
            "options": options,
            "correlation_id": correlation_id,
            "reply_to": reply_to,
            "data": data,
        }
    )
    # An empty key means "no key" on handoffs, as it always has.
    headers = _idempotency_headers(idempotency_key) if idempotency_key else {}
    result = yield Request("POST", "/api/agent/handoffs", body=body, headers=headers)
    return result


def handoffs_request_ack(
    prompt: str,
    *,
    target: Optional[str] = None,
    expires_in: Optional[int] = None,
    urgency: Optional[str] = None,
    correlation_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Op[Any]:
    require_non_empty(prompt, "prompt")
    assert_structured_data(data)
    refuse_ping_actions_data(data, "A handoff")
    result = yield from _handoff_create(
        "ack",
        prompt,
        target=target,
        expires_in=expires_in,
        urgency=urgency,
        correlation_id=correlation_id,
        reply_to=reply_to,
        data=data,
        idempotency_key=idempotency_key,
    )
    return result


def handoffs_ask(
    prompt: str,
    options: Sequence[Any],
    *,
    target: Optional[str] = None,
    expires_in: Optional[int] = None,
    urgency: Optional[str] = None,
    correlation_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Op[Any]:
    require_non_empty(prompt, "prompt")
    assert_structured_data(data)
    refuse_ping_actions_data(data, "A handoff")
    if not isinstance(options, (list, tuple)) or len(options) < 2 or len(options) > 4:
        raise invalid_request(
            "`options` must contain between 2 and 4 entries for a question handoff."
        )
    result = yield from _handoff_create(
        "question",
        prompt,
        target=target,
        expires_in=expires_in,
        urgency=urgency,
        correlation_id=correlation_id,
        reply_to=reply_to,
        data=data,
        idempotency_key=idempotency_key,
        options=_normalize_handoff_options(options),
    )
    return result


def handoffs_get(handoff_id: str) -> Op[Any]:
    require_non_empty(handoff_id, "handoff_id")
    result = yield Request("GET", f"/api/agent/handoffs/{_enc(handoff_id)}")
    return result


def _handoff_wait(handoff_id: str, timeout: Optional[int], cancel: Any) -> Op[Any]:
    result = yield Request(
        "GET",
        f"/api/agent/handoffs/{_enc(handoff_id)}/wait",
        query=_query(timeout=timeout),
        timeout=wait_timeout(timeout),
        cancel=cancel,
    )
    return result


def handoffs_wait_for_result(
    handoff_id: str,
    *,
    timeout: Optional[int] = None,
    cancel: Any = None,
    on_retry: Optional[Callable[[PingRoomError, int], None]] = None,
) -> Op[Any]:
    require_non_empty(handoff_id, "handoff_id")
    _assert_on_retry(on_retry)
    result = yield from poll_until_settled(
        poll=lambda: _handoff_wait(handoff_id, timeout, cancel),
        validate=lambda value: _validate_handoff_wait(value, handoff_id),
        settled=lambda handoff: handoff["state"] in _HANDOFF_TERMINAL,
        expires_at=lambda handoff: handoff.get("expires_at"),
        what=f"handoff {handoff_id}",
        min_interval=MIN_HANDOFF_POLL_INTERVAL,
        cancel=cancel,
        on_retry=on_retry,
    )
    return result


def handoffs_list(*, state: Optional[str] = None) -> Op[Any]:
    result = yield Request("GET", "/api/agent/handoffs", query=_query(state=state))
    return _unwrap_list(result, "handoffs")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def metrics_list(invite_code: str) -> Op[Any]:
    result = yield Request("GET", f"/api/agent/rooms/{_enc(invite_code)}/metrics")
    return result


def metrics_set(
    invite_code: str,
    key: str,
    value: Any,
    *,
    label: Optional[str] = None,
    format: Optional[str] = None,
    unit: Optional[str] = None,
) -> Op[Any]:
    normalized_key = normalize_metric_key(key)
    fields = _drop_none({"value": value, "label": label, "format": format, "unit": unit})
    assert_metric_fields(fields, require_value=True)
    result = yield Request(
        "PUT",
        f"/api/agent/rooms/{_enc(invite_code)}/metrics/{_enc(normalized_key)}",
        body=fields,
    )
    return result


def metrics_delete(invite_code: str, key: str) -> Op[None]:
    normalized_key = normalize_metric_key(key)
    yield Request("DELETE", f"/api/agent/rooms/{_enc(invite_code)}/metrics/{_enc(normalized_key)}")
    return None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def attachments_upload(
    content: Any, filename: str, *, content_type: Optional[str] = None
) -> Op[Any]:
    require_non_empty(filename, "filename")
    if isinstance(content, str):
        raw = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray, memoryview)):
        raw = bytes(content)
    else:
        raise invalid_request("`content` must be bytes or a string.")
    # The server answers an oversized body with 413 before reading it; refuse
    # locally so no bytes travel for a file that can never be accepted.
    if len(raw) > ATTACHMENT_MAX_BYTES:
        raise PingRoomError(
            "Attachments are at most 5 MiB each.", code="attachment_too_large", status=413
        )
    result = yield Request(
        "POST",
        "/api/agent/attachments",
        multipart=Multipart("file", filename, raw, content_type or "application/octet-stream"),
    )
    if not isinstance(result, dict) or not isinstance(result.get("attachment"), dict):
        raise PingRoomError(
            "PingRoom returned an invalid attachment upload response.", code="invalid_response"
        )
    return result["attachment"]


def attachments_content(attachment_id: str, *, cancel: Any = None) -> Op[Any]:
    require_non_empty(attachment_id, "attachment_id")
    result = yield Request(
        "GET",
        f"/api/agent/attachments/{_enc(attachment_id)}/content",
        mode="raw",
        cancel=cancel,
    )
    return result


def attachments_manifest(attachment_id: str) -> Op[Any]:
    require_non_empty(attachment_id, "attachment_id")
    result = yield Request("GET", f"/api/agent/attachments/{_enc(attachment_id)}/manifest")
    if isinstance(result, dict):
        return result.get("manifest")
    return None


def attachments_delete(attachment_id: str) -> Op[None]:
    require_non_empty(attachment_id, "attachment_id")
    yield Request("DELETE", f"/api/agent/attachments/{_enc(attachment_id)}")
    return None


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------

#: What each action's 200 receipt may say, as ``(claim.status, stream state)``
#: pairs. ``release`` has two: letting go reopens a request that can still be
#: taken, but a request past its deadline (or four hours old with none) ends as
#: ``expired`` with stream state ``failed``, exactly as time ends an open one.
#: That is still a 200, and a retry replays it. Only ``release`` can end that way.
CLAIM_OUTCOMES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "claim": (("claimed", "running"),),
    "release": (("open", "running"), ("expired", "failed")),
    "done": (("done", "done"),),
    "cancel": (("cancelled", "failed"),),
}


def _validate_claim_result(value: Any, notification_id: str, action: str) -> Dict[str, Any]:
    expected = CLAIM_OUTCOMES[action]
    claim = value.get("claim") if isinstance(value, dict) else None
    party_present = isinstance(claim, dict) and "by" in claim
    party = claim.get("by") if isinstance(claim, dict) else None
    at_present = isinstance(claim, dict) and "at" in claim
    at = claim.get("at") if isinstance(claim, dict) else None
    valid = (
        isinstance(value, dict)
        and _same_id(value.get("notification_id"), notification_id)
        and _is_non_empty_string(value.get("correlation_id"))
        and isinstance(value.get("idempotent"), bool)
        and isinstance(claim, dict)
        and any(
            claim.get("status") == status and value.get("state") == state
            for status, state in expected
        )
        and isinstance(claim.get("late"), bool)
        and party_present
        and (
            party is None
            or (
                isinstance(party, dict)
                and _is_non_empty_string(party.get("id"))
                and isinstance(party.get("name"), str)
            )
        )
        and at_present
        and (at is None or (is_number(at) and math.isfinite(at)))
    )
    if not valid:
        raise PingRoomError(
            "PingRoom returned an invalid claim transition receipt.",
            code="invalid_response",
            body={"expected_notification_id": notification_id, "action": action},
        )
    return cast(Dict[str, Any], value)


def live_push(invite_code: str, ping: Mapping[str, Any]) -> Op[Any]:
    require_non_empty(invite_code, "invite_code")
    if not isinstance(ping, Mapping):
        raise invalid_request("`ping` must be a mapping built with the live_status helpers.")
    require_non_empty(ping.get("correlation_id"), "correlation_id")
    assert_structured_data(ping.get("data"))
    refuse_ping_actions_data(ping.get("data"), "A live-progress stream")
    assert_tags(ping.get("tags"))
    result = yield Request("POST", f"/api/agent/rooms/{_enc(invite_code)}/live", body=dict(ping))
    return result


def live_get(invite_code: str, correlation_id: str) -> Op[Any]:
    """Read back a stream this credential started.

    Resolves ``None`` ONLY for a 404 ``stream_not_found``: no stream with that
    correlation id in the last 24 hours. Every other failure raises: a 404 for
    an unknown or deleted room code, and a 403 ``room_not_granted``, both
    propagate rather than being reported as "no stream".
    """
    require_non_empty(invite_code, "invite_code")
    require_non_empty(correlation_id, "correlation_id")
    try:
        result = yield Request(
            "GET", f"/api/agent/rooms/{_enc(invite_code)}/live/{_enc(correlation_id)}"
        )
    except PingRoomError as error:
        if error.status == 404 and error.code == "stream_not_found":
            return None
        raise
    return result


def live_claim(invite_code: str, notification_id: str, action: str) -> Op[Any]:
    require_non_empty(invite_code, "invite_code")
    require_non_empty(notification_id, "notification_id")
    if not isinstance(action, str) or action not in CLAIM_OUTCOMES:
        raise invalid_request("`action` must be claim, release, done or cancel.")
    result = yield Request(
        "POST",
        f"/api/agent/rooms/{_enc(invite_code)}/live/{_enc(notification_id)}/claim",
        body={"action": action},
    )
    return _validate_claim_result(result, notification_id, action)
