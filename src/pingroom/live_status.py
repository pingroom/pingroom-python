"""Live-status streams: the wire contract for driving a Live Activity, and
builders for every template.

A stream is keyed by ``correlation_id``: the first ping starts a live card on
every room member's lock screen (one alert), further ``running`` pings move it
silently, and the first ``done``/``failed`` sends one completion alert and ends
it.

Each builder returns a body you can hand to ``client.live.push()`` or put in an
incoming webhook payload (``send_incoming_webhook``)::

    client.live.push("ab12cd", live_status.progress("deploy-41", {"state": "running", "progress": 0.4}))
    client.live.push("ab12cd", live_status.done("deploy-41", "Live"))

A **claim request** ("Who's on it?", template ``claim``) is a stream one member
takes on. It starts like any stream, but it then moves only through
``client.live.claim()`` (claim, release, done, cancel), not through further
``live_status`` legs.

``template`` and ``steps`` are fixed at stream creation: a later ping cannot
re-template a running card, and a ``running`` ping whose ``steps`` list differs
from the stored one is rejected with 422 ``steps_immutable``. ``segments``
(``progress``) and ``started_at`` (``timer``) are fixed the same way
(``segments_immutable``, ``started_at_immutable``). Content set at creation is
sticky, so a sparse update carrying just ``progress`` keeps rendering the full
template, and ``{"state": "done"}`` alone keeps the final frame's content.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict, cast

__all__ = [
    "LIVE_STATUS_LIMITS",
    "LIVE_TEMPLATES",
    "LiveStatus",
    "LiveStatusPing",
    "alert",
    "claim",
    "countdown",
    "done",
    "failed",
    "matchup",
    "metrics",
    "progress",
    "question",
    "stats",
    "status",
    "steps",
    "timer",
    "value",
]

#: Every ``live_status.template`` the server accepts, in the frozen wire order
#: (pinned to ``docs/contracts/live-templates.json``). An app that does not
#: know one shows the ``status`` layout (title and message), so a template added
#: later never leaves a card blank.
LIVE_TEMPLATES: Tuple[str, ...] = (
    "status",
    "steps",
    "progress",
    "metrics",
    "countdown",
    "question",
    "matchup",
    "claim",
    "timer",
    "value",
    "stats",
)

#: The server's limits on the fields the ``timer``, ``value``, ``stats`` and
#: segmented ``progress`` layouts added. The SDK does not enforce them (the
#: server answers 422 with the rule it broke); they are here for a caller that
#: wants to check before sending. Lengths are in characters, as Laravel counts them.
LIVE_STATUS_LIMITS: Mapping[str, int] = {
    # ``value``: the ``value`` string.
    "value_length": 24,
    # ``value``: the ``value_label`` string.
    "value_label_length": 24,
    # ``value``: the ``delta`` string.
    "delta_length": 12,
    # ``stats``: fewest and most ``{label, value}`` pairs.
    "stats_min": 4,
    "stats_max": 8,
    # ``stats``: each pair's ``label`` and ``value``.
    "stat_label_length": 24,
    "stat_value_length": 24,
    # ``progress``: ``segments`` is 2 to 20; ``current_segment`` is 0 to ``segments``.
    "segments_min": 2,
    "segments_max": 20,
}


class _LiveStatusRequired(TypedDict):
    #: ``running``, ``done`` or ``failed``.
    state: str


class LiveStatus(_LiveStatusRequired, total=False):
    """The ``live_status`` object. Only ``state`` is required; every other field
    is read by the template you selected."""

    template: str
    #: Legacy rendering category (``status``, ``steps`` or ``alert``). Prefer ``template``.
    category: str
    message: str
    #: 0..1: the progress bar / Dynamic Island gauge.
    progress: float
    #: 2-8 labels. Required on the first ping of a ``steps`` stream; immutable after.
    steps: List[str]
    #: Index into ``steps``; the only mutable steps field.
    current_step: int
    #: Up to 3 ``{"label", "value"}`` counters, for the ``metrics`` template.
    metrics: List[Dict[str, str]]
    #: Epoch seconds the ``countdown`` template counts down to.
    deadline_at: int
    #: ``countdown`` only: the timer's length in seconds (read from the first ping).
    duration_seconds: int
    #: ``progress`` only: draw the bar in this many equal segments (2-20). Immutable once set.
    segments: int
    #: ``progress`` only: how many segments are complete, 0..``segments``.
    current_segment: int
    #: ``timer`` only: epoch seconds the card counts up from. Immutable after.
    started_at: int
    #: ``value`` only, required on the first ping: the one prominent value (max 24 characters).
    value: str
    #: ``value`` only: what the value is (max 24 characters).
    value_label: str
    #: ``value`` only: the change, such as ``+3.2%`` (max 12 characters).
    delta: str
    #: ``value`` only: which way ``delta`` moved (``up``, ``down`` or ``flat``).
    trend: str
    #: ``stats`` only, required on the first ping: 4 to 8 ``{"label", "value"}`` pairs.
    stats: List[Dict[str, str]]
    #: Epoch seconds; renders a live ETA on ``status``/``progress``.
    eta_at: int
    #: The ask, for the ``question`` template.
    prompt: str
    #: Up to 4 ``{"value", "label"}`` choices, for the ``question`` template.
    options: List[Dict[str, str]]
    left: Dict[str, Any]
    right: Dict[str, Any]
    center: str
    #: Hex ``#rrggbb``: a semantic accent for one frame.
    accent_override: str
    agent_id: str


class _LiveStatusPingRequired(TypedDict):
    correlation_id: str
    live_status: LiveStatus


class LiveStatusPing(_LiveStatusPingRequired, total=False):
    """The full body of a live-status ping."""

    title: str
    #: Quick-action slot 1-16 supplying the icon and sound.
    action: int
    data: Dict[str, Any]
    #: Up to 10 labels, set when the first ping creates the stream and never changed.
    tags: List[str]
    requires_ack: bool
    ack_timeout_seconds: int


def _ping(
    correlation_id: str,
    live: Mapping[str, Any],
    title: Optional[str],
    action: Optional[int],
    data: Optional[Dict[str, Any]],
    tags: Optional[Sequence[str]],
    requires_ack: Optional[bool],
    ack_timeout_seconds: Optional[int],
) -> LiveStatusPing:
    body: Dict[str, Any] = {"correlation_id": correlation_id, "live_status": dict(live)}
    for key, value in (
        ("title", title),
        ("action", action),
        ("data", data),
        ("tags", list(tags) if tags is not None else None),
        ("requires_ack", requires_ack),
        ("ack_timeout_seconds", ack_timeout_seconds),
    ):
        if value is not None:
            body[key] = value
    return cast(LiveStatusPing, body)


def _templated(
    template: str,
    correlation_id: str,
    live: Mapping[str, Any],
    title: Optional[str],
    action: Optional[int],
    data: Optional[Dict[str, Any]],
    tags: Optional[Sequence[str]],
    requires_ack: Optional[bool],
    ack_timeout_seconds: Optional[int],
) -> LiveStatusPing:
    return _ping(
        correlation_id,
        {"template": template, **live},
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def status(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Default template: a title line plus a status dot, with optional progress."""
    return _templated(
        "status", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def steps(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """A segmented step tracker. ``steps`` is required on the first ping."""
    return _templated(
        "steps", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def progress(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """A determinate progress bar with an optional live ETA.

    Set ``segments`` (2-20) and ``current_segment`` to draw it in equal
    segments; ``progress`` is then derived as ``current_segment / segments``
    when you omit it.
    """
    return _templated(
        "progress",
        correlation_id,
        live,
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def metrics(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Up to 3 labelled counters."""
    return _templated(
        "metrics",
        correlation_id,
        live,
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def countdown(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """A large live timer counting down to ``deadline_at``."""
    return _templated(
        "countdown",
        correlation_id,
        live,
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def question(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """A prompt with up to 4 options shown on the lock screen."""
    return _templated(
        "question",
        correlation_id,
        live,
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def matchup(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Two sides plus a center score/clock."""
    return _templated(
        "matchup",
        correlation_id,
        live,
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def claim(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """ "Who's on it?": a request one room member takes on.

    Private standard rooms only (``422 claim_private_rooms_only`` elsewhere).
    Pass the request text as ``title`` (1 to 120 characters; ``live["message"]``
    is used when there is no title). Send ``state: "running"`` on the first
    ping. ``deadline_at`` is optional, in the future and at most 8 hours away.
    Anything else in ``live`` is ignored, including a ``claim`` block.

    After creation the request moves with ``client.live.claim()``. A producer
    without a member identity (an incoming webhook) ends its own request with
    ``done()`` (done) or ``failed()`` (cancelled).
    """
    return _templated(
        "claim", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def timer(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """A timer counting UP from ``started_at``, kept by the device.

    No per-second pushes are needed. ``started_at`` defaults to the first
    ping's time. A finished timer freezes at its final elapsed time. As with
    every stream, send an update at least every 15 minutes or it is ended as stale.
    """
    return _templated(
        "timer", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def value(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """One prominent value.

    ``value`` is required on the first ping; ``value_label``, ``delta`` and
    ``trend`` are optional. When you send no ``message`` the server writes one
    ("Label Value (delta)") so an older app, which shows the status layout,
    still reads well.
    """
    return _templated(
        "value", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def stats(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Four to eight labelled values (``live["stats"]``), required on the first ping.

    Use ``metrics`` for up to 3. When you send no ``message`` the server writes
    one ("Label Value · Label Value ...") for older apps.
    """
    return _templated(
        "stats", correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds
    )


def alert(
    correlation_id: str,
    live: LiveStatus,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """An urgent stream, delivered time-sensitive from the start so it breaks through Focus.

    This is a ``category``, not a ``template``: ``alert`` has no template
    equivalent, and it is the only way to start a STREAM time-sensitive. Fixed
    at creation, like ``template``.

    (``requires_ack`` is unrelated: it demands somebody tap, and does not raise
    the interruption level.)
    """
    return _ping(
        correlation_id,
        {"category": "alert", **live},
        title,
        action,
        data,
        tags,
        requires_ack,
        ack_timeout_seconds,
    )


def done(
    correlation_id: str,
    message: Optional[str] = None,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Finish a stream. The one alert your stream earns."""
    live: Dict[str, Any] = {"state": "done"}
    if message:
        live["message"] = message
    return _ping(correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds)


def failed(
    correlation_id: str,
    message: Optional[str] = None,
    *,
    title: Optional[str] = None,
    action: Optional[int] = None,
    data: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    requires_ack: Optional[bool] = None,
    ack_timeout_seconds: Optional[int] = None,
) -> LiveStatusPing:
    """Finish a stream unsuccessfully."""
    live: Dict[str, Any] = {"state": "failed"}
    if message:
        live["message"] = message
    return _ping(correlation_id, live, title, action, data, tags, requires_ack, ack_timeout_seconds)
