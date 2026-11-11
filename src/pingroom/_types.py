"""Result types.

Every result is a plain ``dict`` at runtime: the JSON the API sent, untouched,
so a field the server adds later is still there. The ``TypedDict`` classes
describe the fields this SDK knows about, for editors and type checkers. A key
marked as not required may be missing from an older server's answer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict, Union

__all__ = [
    "AcknowledgementWaitResult",
    "ActionState",
    "AgentNotification",
    "Approval",
    "Attachment",
    "AttachmentContent",
    "Handoff",
    "IncomingWebhookResult",
    "JsonObject",
    "ListMetricsResult",
    "LiveClaimResult",
    "LiveStatusResult",
    "LiveStatusSnapshot",
    "NotificationDetail",
    "Paginated",
    "PingAction",
    "PingResult",
    "Question",
    "RoomMetric",
    "WaitResult",
    "ZipManifest",
]

#: A plain JSON object: the shape the API accepts for structured ``data``.
JsonObject = Dict[str, Any]


class PingAction(TypedDict, total=False):
    """A button as every reader of the Ping sees it.

    An ``open_url`` button carries its link and the host the server derived
    from it; a ``webhook`` button never says where it goes.
    """

    id: str
    label: str
    type: str
    style: str
    url: str
    host: str


class PingActionRun(TypedDict, total=False):
    action_id: str
    status: str
    ran_at: Optional[str]


class _ActionAcker(TypedDict, total=False):
    id: Optional[str]
    name: Optional[str]
    profile_image: Optional[str]


class ActionState(TypedDict, total=False):
    """The server-authoritative acknowledgement lifecycle attached to a Ping."""

    mode: str
    confirmed_count: int
    required_count: int
    confirmed_user_ids: List[str]
    status: Optional[str]
    requires_ack: bool
    acked_by: Optional[_ActionAcker]
    acked_at: Optional[str]
    expires_at: Optional[str]
    ended_at: Optional[str]
    can_ack: bool
    dismissed_count: int
    dismissed_user_ids: List[str]
    dismissed_at: Optional[str]


class Attachment(TypedDict):
    """Safe attachment metadata. Read the bytes with ``attachments.content()``."""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: str


class AttachmentContent(TypedDict):
    """The bytes of an attachment and the type the server gave them."""

    content: bytes
    content_type: Optional[str]


class _ZipManifestEntryRequired(TypedDict):
    name: str
    size_bytes: Optional[int]
    is_directory: bool


class ZipManifest(TypedDict):
    """A ``.zip`` attachment's contents as recorded at upload (bounded server-side)."""

    entries: List[_ZipManifestEntryRequired]
    total_entries: int
    truncated: bool
    total_uncompressed_bytes: int


class _PingResultRequired(TypedDict):
    id: str
    message: str
    action_number: Optional[int]
    action_icon: Optional[str]
    trigger_source: Optional[str]
    created_at: str


class PingResult(_PingResultRequired, total=False):
    """What a send resolves with. Unknown fields are kept."""

    data: Optional[JsonObject]
    correlation_id: Optional[str]
    tags: List[str]
    reply_to: Optional[str]
    attachments: List[Attachment]
    action_state: Optional[ActionState]
    edited_at: Optional[str]
    replaced: bool
    withdrawn_at: Optional[str]
    recipient_count: int
    muted_count: int
    confirmation_reach: Dict[str, Any]
    actions: List[PingAction]
    actions_expire_at: str
    action_runs: List[PingActionRun]
    #: The secret every delivery of this Ping's ``webhook`` buttons is signed
    #: with. Only on the send result, only when the Ping has a ``webhook``
    #: button, never on any read: keep it.
    action_signing_secret: str


class _AgentNotificationRequired(TypedDict):
    id: str
    message: str
    created_at: str


class AgentNotification(_AgentNotificationRequired, total=False):
    """A listened or listed notification. Unknown fields are kept."""

    action_number: Optional[int]
    action_icon: Optional[str]
    trigger_source: Optional[str]
    is_handoff: bool
    data: Optional[JsonObject]
    correlation_id: Optional[str]
    tags: List[str]
    reply_to: Optional[str]
    attachments: List[Attachment]
    action_state: Optional[ActionState]
    edited_at: Optional[str]
    withdrawn_at: Optional[str]
    ack_mode: str
    is_silent: Optional[bool]
    question: Optional["Question"]
    room: Dict[str, Any]
    sender: Dict[str, Any]
    origin_agent: Optional[Dict[str, Any]]
    actions: List[PingAction]
    actions_expire_at: str
    action_runs: List[PingActionRun]


class NotificationDetail(AgentNotification, total=False):
    """The full notification record of ``notifications.get()``."""

    room_id: str
    sender_id: str
    type: str
    requires_ack: bool
    is_urgent: bool


class Paginated(TypedDict, total=False):
    """A page of ``notifications.list()``; other pagination fields are kept."""

    current_page: int
    data: List[NotificationDetail]
    per_page: int
    total: int
    last_page: int
    next_page_url: Optional[str]
    prev_page_url: Optional[str]


class WaitResult(TypedDict):
    notifications: List[AgentNotification]
    cursor: str


class AcknowledgementWaitResult(TypedDict):
    id: str
    action_state: ActionState


class Approval(TypedDict, total=False):
    """An approval. ``status`` is ``pending``, ``decided``, ``expired`` or ``cancelled``."""

    id: str
    status: str
    question: str
    title: Optional[str]
    options: List[str]
    decision: Optional[str]
    decided_at: Optional[str]
    expires_at: Optional[str]
    correlation_id: Optional[str]
    data: Optional[JsonObject]
    room_code: Optional[str]


class Question(TypedDict, total=False):
    """A Question or poll: the same wire shape across ask, get, wait and list."""

    id: str
    notification_id: Optional[str]
    kind: str
    prompt: str
    context: Optional[str]
    options: List[Dict[str, Any]]
    text_input: Optional[Dict[str, Any]]
    responder_scope: str
    target_user_id: Optional[str]
    resolution_policy: str
    state: str
    answer: Optional[Dict[str, Any]]
    results: Optional[Dict[str, Any]]
    expires_at: Optional[str]
    resolved_at: Optional[str]
    correlation_id: Optional[str]
    reply_to: Optional[str]
    data: Optional[JsonObject]
    created_at: Optional[str]
    room: Dict[str, Any]
    room_code: str
    asker: Dict[str, Any]


class Handoff(TypedDict, total=False):
    """A handoff: the same wire shape across create, get, wait and list."""

    id: str
    kind: str
    prompt: str
    state: str
    delivery_state: Optional[str]
    correlation_id: Optional[str]
    reply_to: Optional[str]
    data: Optional[JsonObject]
    expires_at: Optional[str]
    created_at: Optional[str]
    acked_by: Optional[Dict[str, Any]]
    acked_at: Optional[str]
    options: List[Dict[str, Any]]
    answer: Optional[Dict[str, Any]]
    activation_completed: bool


class RoomMetric(TypedDict):
    """One metric as the API returns it. ``value`` is raw, never pre-formatted."""

    key: str
    label: str
    format: str
    unit: Optional[str]
    value: Union[int, float, str, None]
    value_updated_at: Optional[str]


class ListMetricsResult(TypedDict):
    metrics: List[RoomMetric]
    limit: int


class LiveStatusResult(TypedDict, total=False):
    notification_id: str
    correlation_id: str
    #: ``started`` on creation, then ``running`` / ``done`` / ``failed``.
    state: str
    idempotent: bool
    action_state: Any


class LiveStatusSnapshot(TypedDict, total=False):
    """The read-back shape of a stream: every stored ``live_status`` field.

    Fields you never set come back as ``None`` rather than being omitted.
    """

    notification_id: str
    correlation_id: str
    tags: List[str]
    state: Optional[str]
    progress: Optional[float]
    message: Optional[str]
    category: Optional[str]
    template: Optional[str]
    agent_id: Optional[str]
    accent_override: Optional[str]
    eta_at: Optional[int]
    deadline_at: Optional[int]
    duration_seconds: Optional[int]
    metrics: Optional[List[Dict[str, str]]]
    prompt: Optional[str]
    options: Optional[List[Dict[str, str]]]
    left: Optional[Dict[str, Any]]
    right: Optional[Dict[str, Any]]
    center: Optional[str]
    steps: Optional[List[str]]
    current_step: Optional[int]
    segments: Optional[int]
    current_segment: Optional[int]
    started_at: Optional[int]
    value: Optional[str]
    value_label: Optional[str]
    delta: Optional[str]
    trend: Optional[str]
    stats: Optional[List[Dict[str, str]]]
    action_state: Any
    updated_at: Optional[str]
    title: str
    claim: Dict[str, Any]


class LiveClaimResult(TypedDict):
    """The 200 body of ``live.claim()``."""

    notification_id: str
    correlation_id: str
    claim: Dict[str, Any]
    state: str
    idempotent: bool


class IncomingWebhookResult(TypedDict, total=False):
    success: bool
    notification_id: str
    replaced: bool
    message: str
    action_state: Optional[ActionState]
    metric: RoomMetric
    unchanged: bool
    actions: List[PingAction]
    action_signing_secret: str
