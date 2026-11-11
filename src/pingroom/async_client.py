"""The asynchronous client: the same API as ``PingRoom``, on ``httpx.AsyncClient``.

Every method here does what its blocking twin does, through the same operations
(``pingroom._ops``); only the I/O differs. Cancel an ``await`` by cancelling the
task, or pass an ``asyncio.Event`` as ``cancel`` to a wait or ``listen()``.
"""

from __future__ import annotations

import asyncio
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
    cast,
)

import httpx

from . import _ops as ops
from ._core import DEFAULT_TIMEOUT, UNSET
from ._driver import AsyncDriver
from ._types import (
    AcknowledgementWaitResult,
    AgentNotification,
    Approval,
    Attachment,
    AttachmentContent,
    Handoff,
    JsonObject,
    ListMetricsResult,
    LiveClaimResult,
    LiveStatusResult,
    LiveStatusSnapshot,
    NotificationDetail,
    Paginated,
    PingResult,
    Question,
    RoomMetric,
    WaitResult,
    ZipManifest,
)
from .client import OnRetry, render, resolve_config
from .errors import PingRoomError

__all__ = ["AsyncPingRoom"]


class AsyncNotifications:
    """``client.notifications``: history, one-shot waits and the ``listen()`` iterator."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def list(
        self,
        *,
        type: Optional[str] = None,
        room_id: Optional[str] = None,
        tag: Optional[str] = None,
        date: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        notifications_per_page: Optional[int] = None,
    ) -> Paginated:
        """Paginated history (a polling fallback; prefer ``wait()`` and ``listen()``).

        ``type`` is ``received`` or ``sent``. ``limit`` is 1-25 (omit it for the
        server's legacy default of 50); the deprecated ``notifications_per_page``
        maps to ``limit`` and ``limit`` wins. Each row's ``room`` keeps the API's
        room object and gains ``code`` (the invite code), as ``listen()`` rows have.
        """
        return cast(
            Paginated,
            await self._driver.run(
                ops.notifications_list(
                    type=type,
                    room_id=room_id,
                    tag=tag,
                    date=date,
                    limit=limit,
                    page=page,
                    notifications_per_page=notifications_per_page,
                )
            ),
        )

    async def get(self, notification_id: str) -> NotificationDetail:
        """Fetch one visible notification, including its current acknowledgement state."""
        return cast(
            NotificationDetail, await self._driver.run(ops.notifications_get(notification_id))
        )

    async def replace(
        self,
        invite_code: str,
        notification_id: str,
        message: str,
        *,
        title: Union[str, Any, None] = UNSET,
        data: Union[JsonObject, Any, None] = UNSET,
    ) -> PingResult:
        """Replace the title, message and data of a Ping this credential already sent.

        The notification on each recipient's phone changes in place, silently.
        ``title`` and ``data`` keep their current value when left out; ``None``
        clears them. Only an ordinary Ping you sent in a private room, under 24
        hours old and replaced fewer than 20 times can be replaced; each refusal
        has its own ``error.code`` (``REPLACE_ERROR_CODES``).
        """
        return cast(
            PingResult,
            await self._driver.run(
                ops.notifications_replace(
                    invite_code, notification_id, message, title=title, data=data
                )
            ),
        )

    async def withdraw(self, invite_code: str, notification_id: str) -> PingResult:
        """Take back a Ping this credential already sent.

        Its notification leaves each recipient's Lock Screen and Notification
        Center; the Ping stays in the room's history with ``withdrawn_at`` set.
        Withdrawing twice is a harmless no-op. Only the Pings that can be
        replaced can be withdrawn (``WITHDRAW_ERROR_CODES``).
        """
        return cast(
            PingResult,
            await self._driver.run(ops.notifications_withdraw(invite_code, notification_id)),
        )

    async def wait_for_acknowledgement(
        self,
        notification_id: str,
        *,
        timeout: Optional[int] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> AcknowledgementWaitResult:
        """One bounded long-poll for an ack-required Ping.

        The returned state may still be ``open`` when the hold timeout elapses;
        call again to keep waiting.
        """
        return cast(
            AcknowledgementWaitResult,
            await self._driver.run(
                ops.notifications_wait_for_acknowledgement(
                    notification_id, timeout=timeout, cancel=cancel
                )
            ),
        )

    async def wait(
        self,
        *,
        after: Optional[str] = None,
        timeout: Optional[int] = None,
        limit: Optional[int] = None,
        tag: Optional[str] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> WaitResult:
        """One long-poll: returns as soon as new Pings land, or with an empty list at timeout.

        Without ``after`` it answers at once with no Pings and the current head
        cursor, so pass the cursor from a previous call.
        """
        return cast(
            WaitResult,
            await self._driver.run(
                ops.notifications_wait(
                    after=after, timeout=timeout, limit=limit, tag=tag, cancel=cancel
                )
            ),
        )

    def listen(
        self,
        *,
        after: Optional[str] = None,
        timeout: Optional[int] = None,
        limit: Optional[int] = None,
        tag: Optional[str] = None,
        cancel: Optional[asyncio.Event] = None,
        on_error: Optional[Callable[[PingRoomError], None]] = None,
        backoff: float = 1.0,
    ) -> AsyncIterator[AgentNotification]:
        """Continuous real-time inbound: an async iterator that long-polls in a loop.

        It reads the current head first, then polls forward, advancing the
        cursor and yielding each new Ping. It ends when ``cancel`` (an
        ``asyncio.Event``) is set, abandoning the poll in flight, or when the
        task is cancelled. Transient failures (network errors, timeouts, 429, 5xx) are
        retried with a backoff that starts at ``backoff`` seconds (minimum 0.25),
        doubles up to 30 s and waits out a server ``retry_after``; ``on_error``
        sees each one. Any other error (a 401 after the credential is revoked, a
        403) ends the iterator by raising. Empty polls start at least 2.1 s apart.
        """
        return cast(
            AsyncIterator[AgentNotification],
            self._driver.iterate(
                ops.notifications_listen(
                    after=after,
                    timeout=timeout,
                    limit=limit,
                    tag=tag,
                    cancel=cancel,
                    on_error=on_error,
                    backoff=backoff,
                )
            ),
        )


class AsyncApprovals:
    """``client.approvals``: two-option human-in-the-loop decisions."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def request(
        self,
        invite_code: str,
        question: str,
        *,
        title: Optional[str] = None,
        options: Optional[Sequence[str]] = None,
        correlation_id: Optional[str] = None,
        data: Optional[JsonObject] = None,
        ttl: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Approval:
        """Ask the human you act for to decide; returns the pending approval.

        Retrying with the same ``idempotency_key`` is safe: the server replays
        the first result instead of asking the human a second time.
        """
        return cast(
            Approval,
            await self._driver.run(
                ops.approvals_request(
                    invite_code,
                    question,
                    title=title,
                    options=options,
                    correlation_id=correlation_id,
                    data=data,
                    ttl=ttl,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    async def get(self, approval_id: str) -> Approval:
        """Fetch an approval's current state."""
        return cast(Approval, await self._driver.run(ops.approvals_get(approval_id)))

    async def wait(
        self,
        approval_id: str,
        *,
        timeout: Optional[int] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> Approval:
        """One long-poll for the decision; returns the (possibly still pending) approval."""
        return cast(
            Approval,
            await self._driver.run(ops.approvals_wait(approval_id, timeout=timeout, cancel=cancel)),
        )

    async def wait_for_decision(
        self,
        approval_id: str,
        *,
        cancel: Optional[asyncio.Event] = None,
        on_retry: Optional[OnRetry] = None,
    ) -> Approval:
        """Block until the human decides (or the request expires or is cancelled).

        Loops ``wait()`` with polls started at least 2.1 s apart. A response
        that is not this approval in a known status raises ``invalid_response``.
        Network failures, 429 and 5xx are retried with backoff (honouring
        ``retry_after``) and reported to ``on_retry``, until ``cancel`` is set or
        the outage outlasts the approval's ``expires_at`` by 5 minutes (15
        minutes of failures while no ``expires_at`` has been read); that raises
        ``unavailable`` with the last failure as its ``__cause__``.
        """
        return cast(
            Approval,
            await self._driver.run(
                ops.approvals_wait_for_decision(approval_id, cancel=cancel, on_retry=on_retry)
            ),
        )


class AsyncQuestions:
    """``client.questions``: AsyncQuestions and polls in a room."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def ask(
        self,
        invite_code: str,
        prompt: str,
        *,
        context: Optional[str] = None,
        options: Optional[Sequence[Union[str, Mapping[str, Any]]]] = None,
        text_input: Optional[Dict[str, Any]] = None,
        responder_scope: Optional[str] = None,
        resolution_policy: Optional[str] = None,
        target_user_id: Optional[str] = None,
        ttl: Optional[int] = None,
        data: Optional[JsonObject] = None,
        correlation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        attachment_ids: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Question:
        """Ask a Question in a room; returns the pending Question.

        Pass ``resolution_policy="all"`` to poll the room: everyone asked votes
        once, and ``results`` carries the tally on every read. Retrying with the
        same ``idempotency_key`` is safe.
        """
        return cast(
            Question,
            await self._driver.run(
                ops.questions_ask(
                    invite_code,
                    prompt,
                    context=context,
                    options=options,
                    text_input=text_input,
                    responder_scope=responder_scope,
                    resolution_policy=resolution_policy,
                    target_user_id=target_user_id,
                    ttl=ttl,
                    data=data,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                    attachment_ids=attachment_ids,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    async def get(self, question_id: str) -> Question:
        """Fetch a Question's current state."""
        return cast(Question, await self._driver.run(ops.questions_get(question_id)))

    async def wait(
        self,
        question_id: str,
        *,
        timeout: Optional[int] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> Question:
        """One long-poll for the answer; returns the (possibly still pending) Question."""
        return cast(
            Question,
            await self._driver.run(ops.questions_wait(question_id, timeout=timeout, cancel=cancel)),
        )

    async def wait_for_answer(
        self,
        question_id: str,
        *,
        cancel: Optional[asyncio.Event] = None,
        on_retry: Optional[OnRetry] = None,
    ) -> Question:
        """Block until the human answers (or the Question expires or is cancelled).

        Returns the resolved Question: inspect ``state`` and ``answer["value"]``.
        Polls start at least 2.1 s apart; retries and bounds are those of
        ``approvals.wait_for_decision()``.
        """
        return cast(
            Question,
            await self._driver.run(
                ops.questions_wait_for_answer(question_id, cancel=cancel, on_retry=on_retry)
            ),
        )

    async def list(self, *, state: Optional[str] = None) -> List[Question]:
        """List the asker's Questions, optionally filtered by ``state``."""
        return cast(List[Question], await self._driver.run(ops.questions_list(state=state)))

    async def cancel(self, question_id: str) -> Question:
        """Withdraw a still-pending Question (409 if it already resolved)."""
        return cast(Question, await self._driver.run(ops.questions_cancel(question_id)))


class AsyncHandoffs:
    """``client.handoffs``: the direct, private agent-to-human loop."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def request_ack(
        self,
        prompt: str,
        *,
        target: Optional[str] = None,
        expires_in: Optional[int] = None,
        urgency: Optional[str] = None,
        correlation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        data: Optional[JsonObject] = None,
        idempotency_key: Optional[str] = None,
    ) -> Handoff:
        """Ask a human to acknowledge (an ``ack`` handoff). Resolves on the first acknowledgement.

        ``target`` is ``"me"`` (the default): the human who connected the agent.
        ``expires_in`` is seconds (the server clamps it to 120-86400, default
        900). ``urgency`` is ``"active"`` or ``"passive"``. An empty
        ``idempotency_key`` sends no key; otherwise it is sent verbatim on
        retries (same key and body replays the original).
        """
        return cast(
            Handoff,
            await self._driver.run(
                ops.handoffs_request_ack(
                    prompt,
                    target=target,
                    expires_in=expires_in,
                    urgency=urgency,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                    data=data,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    async def ask(
        self,
        prompt: str,
        options: Sequence[Union[str, Mapping[str, Any]]],
        *,
        target: Optional[str] = None,
        expires_in: Optional[int] = None,
        urgency: Optional[str] = None,
        correlation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        data: Optional[JsonObject] = None,
        idempotency_key: Optional[str] = None,
    ) -> Handoff:
        """Ask a human a multi-option question (a ``question`` handoff).

        Resolves on the first tapped answer. 2-4 ``options``; a bare string is
        shorthand for ``{"value": s, "label": s}``.
        """
        return cast(
            Handoff,
            await self._driver.run(
                ops.handoffs_ask(
                    prompt,
                    options,
                    target=target,
                    expires_in=expires_in,
                    urgency=urgency,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                    data=data,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    async def get(self, handoff_id: str) -> Handoff:
        """Fetch a handoff's current state."""
        return cast(Handoff, await self._driver.run(ops.handoffs_get(handoff_id)))

    async def wait_for_result(
        self,
        handoff_id: str,
        *,
        timeout: Optional[int] = None,
        cancel: Optional[asyncio.Event] = None,
        on_retry: Optional[OnRetry] = None,
    ) -> Handoff:
        """Block until the handoff resolves (or expires or is cancelled).

        ``expired``, ``cancelled`` and a negative answer (``"hold"``) all come
        back as normal terminal results; nothing is raised for them. ``timeout``
        is the seconds each poll is held (the server caps it at 25, default 20).
        Polls start at least 2.1 s apart; retries and bounds are those of
        ``approvals.wait_for_decision()``.
        """
        return cast(
            Handoff,
            await self._driver.run(
                ops.handoffs_wait_for_result(
                    handoff_id, timeout=timeout, cancel=cancel, on_retry=on_retry
                )
            ),
        )

    async def list(self, *, state: Optional[str] = None) -> List[Handoff]:
        """List the agent's open handoffs (``state="open"``) or recent history (``"all"``)."""
        return cast(List[Handoff], await self._driver.run(ops.handoffs_list(state=state)))


class AsyncMetrics:
    """``client.metrics``: named values a script keeps in a room for a phone widget.

    Setting one sends no Ping and no push.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def list(self, invite_code: str) -> ListMetricsResult:
        """The room's metrics, oldest first, with the per-room limit."""
        return cast(ListMetricsResult, await self._driver.run(ops.metrics_list(invite_code)))

    async def set(
        self,
        invite_code: str,
        key: str,
        value: Union[int, float, str],
        *,
        label: Optional[str] = None,
        format: Optional[str] = None,
        unit: Optional[str] = None,
    ) -> RoomMetric:
        """Set a metric's value, creating the metric when the key is new (an upsert).

        ``format`` is ``number``, ``currency``, ``percent``, ``unit`` or ``text``.
        The value is raw, never pre-formatted (``12400``, not ``"$12,400"``).
        """
        return cast(
            RoomMetric,
            await self._driver.run(
                ops.metrics_set(invite_code, key, value, label=label, format=format, unit=unit)
            ),
        )

    async def delete(self, invite_code: str, key: str) -> None:
        """Delete a metric. Raises ``metric_not_found`` when the room has no such key."""
        await self._driver.run(ops.metrics_delete(invite_code, key))


class AsyncAttachments:
    """``client.attachments``: upload, read and discard private files."""

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def upload(
        self,
        content: Union[bytes, bytearray, memoryview, str],
        filename: str,
        *,
        content_type: Optional[str] = None,
    ) -> Attachment:
        """Upload one file (1 byte to 5 MiB) and get back the id a send can claim.

        The filename's extension must be md, pdf, html, txt, jpg, jpeg, png or
        zip. Uploading needs a Pro account (``402 pro_required`` otherwise). An
        upload that is never attached expires after 24 hours.
        """
        return cast(
            Attachment,
            await self._driver.run(
                ops.attachments_upload(content, filename, content_type=content_type)
            ),
        )

    async def content(
        self, attachment_id: str, *, cancel: Optional[asyncio.Event] = None
    ) -> AttachmentContent:
        """The raw bytes of an attachment this credential may see, with their content type."""
        return cast(
            AttachmentContent,
            await self._driver.run(ops.attachments_content(attachment_id, cancel=cancel)),
        )

    async def manifest(self, attachment_id: str) -> Optional[ZipManifest]:
        """The listing of a ``.zip``'s contents, recorded at upload.

        Returns ``None`` for a non-archive, and for an archive whose central
        directory did not parse. The declared sizes are what the archive claims.
        """
        return cast(
            Optional[ZipManifest], await self._driver.run(ops.attachments_manifest(attachment_id))
        )

    async def delete(self, attachment_id: str) -> None:
        """Discard an upload that has not been attached to a Ping yet."""
        await self._driver.run(ops.attachments_delete(attachment_id))


class AsyncLive:
    """``client.live``: live-status streams, a self-updating card on members' lock screens.

    Build the body of each ping with the helpers in ``pingroom.live_status``.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    async def push(self, invite_code: str, ping: Mapping[str, Any]) -> LiveStatusResult:
        """Send one stream ping: start, update or end, chosen by ``live_status.state``
        and whether the correlation already exists."""
        return cast(LiveStatusResult, await self._driver.run(ops.live_push(invite_code, ping)))

    async def get(self, invite_code: str, correlation_id: str) -> Optional[LiveStatusSnapshot]:
        """Read back a stream this credential started.

        Returns ``None`` only for a 404 ``stream_not_found`` (no stream with that
        correlation id in the last 24 hours). Every other failure raises.
        """
        return cast(
            Optional[LiveStatusSnapshot],
            await self._driver.run(ops.live_get(invite_code, correlation_id)),
        )

    async def claim(self, invite_code: str, notification_id: str, action: str) -> LiveClaimResult:
        """Act on a "Who's on it?" request as the human this credential acts for.

        ``action`` is ``claim``, ``release``, ``done`` or ``cancel``. A refusal
        raises ``PingRoomError`` (``claim_conflict`` 409, ``claim_own_request``
        422, ``claim_private_rooms_only`` 422, ``claim_not_found`` 404 or
        ``forbidden`` 403); on 409 and 422 ``error.body["claim"]`` shows who has
        it. A malformed or mismatched success raises ``invalid_response``; the
        SDK never repeats the transition POST automatically.
        """
        return cast(
            LiveClaimResult,
            await self._driver.run(ops.live_claim(invite_code, notification_id, action)),
        )


class AsyncPingRoom:
    """The asynchronous PingRoom client.

    Args:
        token: The agent credential (a Bearer JWT). Calls that need one raise
            ``no_token`` without it.
        base_url: The API base URL. Defaults to the ``PINGROOM_API_URL`` or
            ``PINGROOM_BASE_URL`` environment variable, else
            ``https://api.pingroom.io``. Plain ``http`` is refused except to a
            loopback host, unless ``allow_insecure`` is set.
        timeout: The per-request timeout in seconds (default 30). Long-poll
            calls (``wait``, ``listen``, ``wait_for_*``) use their hold time
            plus 10 s instead.
        transport: An ``httpx.AsyncBaseTransport``, for tests or special routing.
        user_agent: Overrides the ``User-Agent`` header (default
            ``pingroom-python/<version>``).
        allow_insecure: Allow plain-``http`` base URLs to non-loopback hosts.

    Redirects are never followed. The token is held privately and never appears
    in ``repr()``, ``str()`` or an exception message. Use it as
    ``async with AsyncPingRoom(token) as client:`` or call ``aclose()``.
    """

    notifications: AsyncNotifications
    approvals: AsyncApprovals
    questions: AsyncQuestions
    handoffs: AsyncHandoffs
    metrics: AsyncMetrics
    attachments: AsyncAttachments
    live: AsyncLive

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        user_agent: Optional[str] = None,
        allow_insecure: bool = False,
    ) -> None:
        self._config = resolve_config(token, base_url, timeout, user_agent, allow_insecure)
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        driver = AsyncDriver(self._config, self._http)
        self._driver = driver
        self.notifications = AsyncNotifications(driver)
        self.approvals = AsyncApprovals(driver)
        self.questions = AsyncQuestions(driver)
        self.handoffs = AsyncHandoffs(driver)
        self.metrics = AsyncMetrics(driver)
        self.attachments = AsyncAttachments(driver)
        self.live = AsyncLive(driver)

    @property
    def base_url(self) -> str:
        """The API base URL requests go to."""
        return self._config.base_url

    def set_token(self, token: Optional[str]) -> None:
        """Replace the credential. Pass ``None`` to clear it."""
        self._config.set_token(token)

    def get_token(self) -> Optional[str]:
        """The credential, or ``None``. The only way to read it back."""
        return None if self._config.token is None else self._config.token.reveal()

    async def broadcast(
        self,
        invite_code: str,
        message: str,
        *,
        title: Optional[str] = None,
        data: Optional[JsonObject] = None,
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
    ) -> PingResult:
        """Broadcast a Ping to every member of a room you own.

        ``message`` is at most 120 characters in a private room and 160 in a
        public one (the SDK checks the 160 ceiling); ``title`` at most 40.
        ``data`` is a JSON object (at most 25 keys and 8 KB as PHP encodes it)
        that every read surface echoes; ``link_ping()`` and ``location_ping()``
        build its reserved parts and ``ping_action`` its ``actions``.

        ``is_urgent`` delivers time-sensitive (it breaks through Focus);
        ``requires_ack`` holds the Ping open until someone taps Acknowledge
        (``ack_mode`` ``any`` or ``all``, ``ack_timeout_seconds`` 60-86400). The
        two are independent.

        Pass ``idempotency_key`` and reuse it when retrying after a timeout or
        a network error, so a send the server already committed is replayed
        rather than delivered twice. Pass ``replace_key`` to keep updating one
        Ping: while your earlier Ping with that key is replaceable the call
        replaces it in place and returns that Ping with ``replaced: True``.

        Fields left as ``None`` are not sent.
        """
        return cast(
            PingResult,
            await self._driver.run(
                ops.broadcast(
                    invite_code,
                    message,
                    title=title,
                    data=data,
                    tags=tags,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                    action_number=action_number,
                    action_icon=action_icon,
                    attachment_ids=attachment_ids,
                    requires_ack=requires_ack,
                    ack_mode=ack_mode,
                    is_urgent=is_urgent,
                    ack_timeout_seconds=ack_timeout_seconds,
                    replace_key=replace_key,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    async def aclose(self) -> None:
        """Close the HTTP connections."""
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncPingRoom":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return render("AsyncPingRoom", self._config)
