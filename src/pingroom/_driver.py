"""Drivers: run an operation (see ``_ops``) over ``httpx``.

An operation is a generator that yields the HTTP requests it wants sent (and
the pauses it wants taken) and is resumed with each result. Its logic is written
once, in ``_ops``; the two drivers here only perform the I/O, one blocking and
one on an event loop. A failed call is thrown back into the operation, so its
retry logic sees the same ``PingRoomError`` either way.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Generator, Iterator, List, Optional, TypeVar

import httpx

from ._core import (
    ClientConfig,
    Command,
    Emit,
    Prepared,
    RawResponse,
    Request,
    Sleep,
    error_description,
    interpret,
    prepare,
    redact_url_in_message,
    refuse_redirect,
)
from .errors import (
    PingRoomCancelledError,
    PingRoomError,
    PingRoomNetworkError,
    PingRoomTimeoutError,
)

T = TypeVar("T")
Op = Generator[Command, Any, T]


def _network_error(
    exc: BaseException, redact_url: Optional[str], prefix: str
) -> PingRoomNetworkError:
    reason = error_description(exc)
    if redact_url is not None:
        reason = redact_url_in_message(reason, redact_url)
    return PingRoomNetworkError(f"{prefix}: {reason}")


# ---------------------------------------------------------------------------
# One request, blocking
# ---------------------------------------------------------------------------


def send_sync(
    client: httpx.Client,
    prepared: Prepared,
    *,
    redact_url: Optional[str] = None,
    error_prefix: str = "Network request failed",
) -> RawResponse:
    """Send one prepared request and read the whole answer.

    No redirect is followed or accepted. ``timeout`` bounds each connect, read
    and write, and the body is abandoned once the total time passes it, so a
    server that trickles bytes cannot hold a call open far beyond it.
    """
    deadline = time.monotonic() + prepared.timeout
    response: Optional[httpx.Response] = None
    try:
        request = client.build_request(
            prepared.method,
            prepared.url,
            headers=prepared.headers,
            content=prepared.content,
            files=prepared.files,
            timeout=httpx.Timeout(prepared.timeout),
        )
        response = client.send(request, stream=True, follow_redirects=False)
        refuse_redirect(response.status_code)
        chunks: List[bytes] = []
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise PingRoomTimeoutError()
        return RawResponse(
            status=response.status_code,
            retry_after=response.headers.get("retry-after"),
            content_type=response.headers.get("content-type"),
            body=b"".join(chunks),
        )
    except httpx.TimeoutException:
        raise PingRoomTimeoutError() from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _network_error(exc, redact_url, error_prefix) from None
    finally:
        if response is not None:
            response.close()


# ---------------------------------------------------------------------------
# One request, on an event loop
# ---------------------------------------------------------------------------


async def _send_async_once(
    client: httpx.AsyncClient,
    prepared: Prepared,
    redact_url: Optional[str],
    error_prefix: str,
) -> RawResponse:
    deadline = time.monotonic() + prepared.timeout
    response: Optional[httpx.Response] = None
    try:
        request = client.build_request(
            prepared.method,
            prepared.url,
            headers=prepared.headers,
            content=prepared.content,
            files=prepared.files,
            timeout=httpx.Timeout(prepared.timeout),
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        refuse_redirect(response.status_code)
        chunks: List[bytes] = []
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise PingRoomTimeoutError()
        return RawResponse(
            status=response.status_code,
            retry_after=response.headers.get("retry-after"),
            content_type=response.headers.get("content-type"),
            body=b"".join(chunks),
        )
    except httpx.TimeoutException:
        raise PingRoomTimeoutError() from None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _network_error(exc, redact_url, error_prefix) from None
    finally:
        if response is not None:
            await response.aclose()


async def send_async(
    client: httpx.AsyncClient,
    prepared: Prepared,
    *,
    redact_url: Optional[str] = None,
    error_prefix: str = "Network request failed",
    cancel: Optional[asyncio.Event] = None,
) -> RawResponse:
    """The asynchronous ``send_sync``. ``cancel`` aborts the call in flight."""
    if cancel is None:
        return await _send_async_once(client, prepared, redact_url, error_prefix)
    if cancel.is_set():
        raise PingRoomCancelledError()
    call = asyncio.ensure_future(_send_async_once(client, prepared, redact_url, error_prefix))
    stop = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({call, stop}, return_when=asyncio.FIRST_COMPLETED)
        if call.done():
            return call.result()
        raise PingRoomCancelledError()
    finally:
        for task in (call, stop):
            if not task.done():
                task.cancel()
        await asyncio.gather(call, stop, return_exceptions=True)


# ---------------------------------------------------------------------------
# Pauses
# ---------------------------------------------------------------------------


def sleep_sync(seconds: float, cancel: Any = None) -> None:
    """Sleep, or return early when ``cancel`` (a ``threading.Event``) is set."""
    if seconds <= 0:
        return
    if cancel is None:
        time.sleep(seconds)
    else:
        cancel.wait(seconds)


async def sleep_async(seconds: float, cancel: Any = None) -> None:
    """Sleep, or return early when ``cancel`` (an ``asyncio.Event``) is set."""
    if seconds <= 0:
        # Still yield, so a cancellation or a timer-driven task can run.
        await asyncio.sleep(0)
        return
    if cancel is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(cancel.wait(), seconds)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


class SyncDriver:
    """Runs operations on a blocking ``httpx.Client``."""

    def __init__(self, config: ClientConfig, client: httpx.Client) -> None:
        self.config = config
        self.client = client

    def _execute(self, request: Request) -> Any:
        # A blocking request cannot be interrupted once it is sent, so a cancel
        # is honoured before it goes out (the asynchronous driver abandons the
        # request in flight as well).
        if request.cancel is not None and request.cancel.is_set():
            raise PingRoomCancelledError()
        prepared = prepare(self.config, request)
        return interpret(send_sync(self.client, prepared), request.mode)

    def _steps(self, op: Op[T]) -> Generator[Any, None, T]:
        """Drive ``op``: yield what it emits, return what it returns."""
        value: Any = None
        error: Optional[PingRoomError] = None
        try:
            while True:
                try:
                    command = op.throw(error) if error is not None else op.send(value)
                except StopIteration as stop:
                    return stop.value  # type: ignore[no-any-return]
                value = None
                error = None
                if isinstance(command, Request):
                    try:
                        value = self._execute(command)
                    except PingRoomError as exc:
                        error = exc
                elif isinstance(command, Sleep):
                    sleep_sync(command.seconds, command.cancel)
                elif isinstance(command, Emit):
                    yield command.item
        finally:
            op.close()

    def run(self, op: Op[T]) -> T:
        steps = self._steps(op)
        try:
            while True:
                next(steps)
        except StopIteration as stop:
            return stop.value  # type: ignore[no-any-return]
        finally:
            steps.close()

    def iterate(self, op: Op[Any]) -> Iterator[Any]:
        yield from self._steps(op)


class AsyncDriver:
    """Runs operations on an ``httpx.AsyncClient``."""

    def __init__(self, config: ClientConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    async def _execute(self, request: Request) -> Any:
        prepared = prepare(self.config, request)
        raw = await send_async(self.client, prepared, cancel=request.cancel)
        return interpret(raw, request.mode)

    async def run(self, op: Op[T]) -> T:
        value: Any = None
        error: Optional[PingRoomError] = None
        try:
            while True:
                try:
                    command = op.throw(error) if error is not None else op.send(value)
                except StopIteration as stop:
                    return stop.value  # type: ignore[no-any-return]
                value = None
                error = None
                if isinstance(command, Request):
                    try:
                        value = await self._execute(command)
                    except PingRoomError as exc:
                        error = exc
                elif isinstance(command, Sleep):
                    await sleep_async(command.seconds, command.cancel)
        finally:
            op.close()

    async def iterate(self, op: Op[Any]) -> AsyncIterator[Any]:
        value: Any = None
        error: Optional[PingRoomError] = None
        try:
            while True:
                try:
                    command = op.throw(error) if error is not None else op.send(value)
                except StopIteration:
                    return
                value = None
                error = None
                if isinstance(command, Request):
                    try:
                        value = await self._execute(command)
                    except PingRoomError as exc:
                        error = exc
                elif isinstance(command, Sleep):
                    await sleep_async(command.seconds, command.cancel)
                elif isinstance(command, Emit):
                    yield command.item
        finally:
            op.close()
