"""The official PingRoom SDK for Python.

    from pingroom import PingRoom

    client = PingRoom(token="...")
    client.broadcast("ab12cd", "Deploy shipped")

``PingRoom`` is the blocking client and ``AsyncPingRoom`` the asynchronous one;
they share one implementation. See the README for the rest.
"""

from __future__ import annotations

from . import live_status, ping_action
from ._core import UNSET
from ._ping_actions import (
    MAX_PING_ACTION_BODY_BYTES,
    MAX_PING_ACTION_BODY_KEYS,
    MAX_PING_ACTION_LABEL_LENGTH,
    MAX_PING_ACTION_URL_LENGTH,
    MAX_PING_ACTIONS,
    PING_ACTION_ID_PATTERN,
    PING_ACTION_RUN_WINDOW_HOURS,
    PING_ACTION_STYLES,
    PING_ACTION_TYPES,
)
from ._types import (
    AcknowledgementWaitResult,
    ActionState,
    AgentNotification,
    Approval,
    Attachment,
    AttachmentContent,
    Handoff,
    IncomingWebhookResult,
    JsonObject,
    ListMetricsResult,
    LiveClaimResult,
    LiveStatusResult,
    LiveStatusSnapshot,
    NotificationDetail,
    Paginated,
    PingAction,
    PingResult,
    Question,
    RoomMetric,
    WaitResult,
    ZipManifest,
)
from ._version import __version__
from .async_client import AsyncPingRoom
from .client import PingRoom
from .errors import (
    AGENT_INBOX_ERROR_CODES,
    COMMON_ERROR_CODES,
    HANDOFF_ERROR_CODES,
    LIVE_CLAIM_ERROR_CODES,
    METRIC_ERROR_CODES,
    PING_ACTION_ERROR_CODES,
    QUICK_ACTION_ERROR_CODES,
    REPLACE_ERROR_CODES,
    REQUEST_LIMIT_ERROR_CODES,
    ROOM_SCOPED_ERROR_CODES,
    WITHDRAW_ERROR_CODES,
    PingRoomCancelledError,
    PingRoomError,
    PingRoomNetworkError,
    PingRoomTimeoutError,
)
from .pings import extract_location_ping, link_ping, location_ping
from .webhooks import (
    INCOMING_WEBHOOK_FIELDS,
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_EVENT_HEADER,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_SIGNATURE_V2_HEADER,
    WEBHOOK_TIMESTAMP_HEADER,
    send_incoming_webhook,
    send_incoming_webhook_async,
    verify_webhook_signature,
)

__all__ = [
    "AGENT_INBOX_ERROR_CODES",
    "COMMON_ERROR_CODES",
    "HANDOFF_ERROR_CODES",
    "INCOMING_WEBHOOK_FIELDS",
    "LIVE_CLAIM_ERROR_CODES",
    "MAX_PING_ACTIONS",
    "MAX_PING_ACTION_BODY_BYTES",
    "MAX_PING_ACTION_BODY_KEYS",
    "MAX_PING_ACTION_LABEL_LENGTH",
    "MAX_PING_ACTION_URL_LENGTH",
    "METRIC_ERROR_CODES",
    "PING_ACTION_ERROR_CODES",
    "PING_ACTION_ID_PATTERN",
    "PING_ACTION_RUN_WINDOW_HOURS",
    "PING_ACTION_STYLES",
    "PING_ACTION_TYPES",
    "QUICK_ACTION_ERROR_CODES",
    "REPLACE_ERROR_CODES",
    "REQUEST_LIMIT_ERROR_CODES",
    "ROOM_SCOPED_ERROR_CODES",
    "UNSET",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "WEBHOOK_SIGNATURE_V2_HEADER",
    "WEBHOOK_TIMESTAMP_HEADER",
    "WITHDRAW_ERROR_CODES",
    "AcknowledgementWaitResult",
    "ActionState",
    "AgentNotification",
    "Approval",
    "AsyncPingRoom",
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
    "PingRoom",
    "PingRoomCancelledError",
    "PingRoomError",
    "PingRoomNetworkError",
    "PingRoomTimeoutError",
    "Question",
    "RoomMetric",
    "WaitResult",
    "ZipManifest",
    "__version__",
    "extract_location_ping",
    "link_ping",
    "live_status",
    "location_ping",
    "ping_action",
    "send_incoming_webhook",
    "send_incoming_webhook_async",
    "verify_webhook_signature",
]
