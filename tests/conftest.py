"""Test harness.

Every API test runs against both the blocking and the asynchronous client
(``harness`` is parametrised over ``sync`` and ``async``), so each public method
is checked twice with no duplicated test code. No test touches the network: all
traffic goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import faulthandler
import inspect
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import httpx
import pytest

from pingroom import AsyncPingRoom, PingRoom, _clock, _driver

Handler = Callable[[httpx.Request], httpx.Response]

START = 1_790_000_000.0  # a fixed wall-clock origin for fake time


def reply(
    body: Any = None,
    status: int = 200,
    headers: Optional[Dict[str, str]] = None,
    text: Optional[str] = None,
) -> httpx.Response:
    """A JSON response (or a raw text one)."""
    if text is not None:
        return httpx.Response(status, text=text, headers=headers)
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


@dataclass
class Recorder:
    """A handler that records every request and answers from a route table."""

    routes: Dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    fallback: Optional[Callable[[httpx.Request], httpx.Response]] = None
    requests: List[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        handler = self.routes.get(key) or self.fallback
        if handler is None:
            return httpx.Response(404, json={})
        return handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def query(self, index: int = -1) -> Dict[str, str]:
        return dict(self.requests[index].url.params.items())


def static(
    body: Any = None, status: int = 200, headers: Optional[Dict[str, str]] = None
) -> Handler:
    return lambda request: reply(body, status, headers)


class LoopSafeEvent:
    """An ``asyncio.Event`` stand-in that binds to a loop only when first awaited.

    Before Python 3.10 an ``asyncio.Event`` created outside a running loop is bound
    to the thread's default loop, and a test builds its event before ``asyncio.run``
    starts the loop that uses it. The SDK only needs ``is_set()`` and ``await wait()``.
    """

    def __init__(self) -> None:
        self._flag = False
        self._event: Optional[asyncio.Event] = None

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True
        if self._event is not None:
            self._event.set()

    async def wait(self) -> bool:
        if self._flag:
            return True
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()
        return True


class Harness:
    """Builds a client of one kind and runs a call on it the same way for both."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._clients: List[Any] = []

    @property
    def is_async(self) -> bool:
        return self.kind == "async"

    def client(self, handler: Union[Handler, Recorder, None] = None, **options: Any) -> Any:
        options.setdefault("token", "tok_test")
        if handler is None:
            handler = static({})
        transport = httpx.MockTransport(handler)
        factory = AsyncPingRoom if self.is_async else PingRoom
        client = factory(transport=transport, **options)
        self._clients.append(client)
        return client

    def run(self, client: Any, call: Callable[..., Any]) -> Any:
        """``call(client)`` for the sync client; awaited on a fresh loop for the async one."""
        result = call(client)
        if inspect.isawaitable(result):

            async def go() -> Any:
                return await result

            return asyncio.run(go())
        return result

    def collect(
        self,
        client: Any,
        make_iterator: Callable[..., Any],
        stop_after: Optional[int] = None,
        on_item: Optional[Callable[[Any], None]] = None,
    ) -> List[Any]:
        """Drain an iterator (sync or async) into a list, stopping after ``stop_after`` items."""
        items: List[Any] = []
        if not self.is_async:
            for item in make_iterator(client):
                items.append(item)
                if on_item is not None:
                    on_item(item)
                if stop_after is not None and len(items) >= stop_after:
                    break
            return items

        async def go() -> List[Any]:
            iterator = make_iterator(client)
            try:
                async for item in iterator:
                    items.append(item)
                    if on_item is not None:
                        on_item(item)
                    if stop_after is not None and len(items) >= stop_after:
                        break
            finally:
                await iterator.aclose()
            return items

        return asyncio.run(go())

    def new_cancel(self) -> Any:
        return LoopSafeEvent() if self.is_async else threading.Event()

    def close(self) -> None:
        for client in self._clients:
            if self.is_async:
                asyncio.run(client.aclose())
            else:
                client.close()


@pytest.fixture(autouse=True)
def _watchdog() -> Iterator[None]:
    """A wait loop that regresses can spin on fake time forever; end the run with a traceback instead."""
    faulthandler.dump_traceback_later(60, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture(params=["sync", "async"])
def harness(request: pytest.FixtureRequest) -> Iterator[Harness]:
    h = Harness(request.param)
    yield h
    h.close()


# ---------------------------------------------------------------------------
# Fake time, for wait loops that back off for minutes
# ---------------------------------------------------------------------------


class FakeClock:
    """Replaces the SDK's clocks and sleeps, so minutes of backoff cost nothing.

    A sleep advances fake time instead of waiting. A callback scheduled with
    ``at()`` runs when fake time reaches it; a sleep that its ``cancel`` event
    cuts short returns early, as a real one would.

    Time is kept as an offset from zero, so the arithmetic on intervals is exact:
    ``monotonic()`` starts near zero, and ``now()`` is ``START`` plus the offset.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self.sleeps: List[float] = []
        self._scheduled: List[Tuple[float, Callable[[], None]]] = []

    def now(self) -> float:
        return START + self.offset

    def monotonic(self) -> float:
        return 1000.0 + self.offset

    def elapsed(self) -> float:
        return self.offset

    def reset(self) -> None:
        self.offset = 0.0

    def at(self, offset: float, action: Callable[[], None]) -> None:
        """Run ``action`` once fake time reaches ``offset`` seconds from the origin."""
        self._scheduled.append((offset, action))
        self._scheduled.sort(key=lambda entry: entry[0])

    def sleep(self, seconds: float, cancel: Any = None) -> None:
        self.sleeps.append(seconds)
        if cancel is not None and cancel.is_set():
            return
        target = self.offset + max(0.0, seconds)
        while self._scheduled and self._scheduled[0][0] <= target:
            when, action = self._scheduled.pop(0)
            self.offset = max(self.offset, when)
            action()
            if cancel is not None and cancel.is_set():
                return
        self.offset = target

    async def sleep_async(self, seconds: float, cancel: Any = None) -> None:
        self.sleep(seconds, cancel)
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(_clock, "now", fake.now)
    monkeypatch.setattr(_clock, "monotonic", fake.monotonic)
    monkeypatch.setattr(_driver, "sleep_sync", fake.sleep)
    monkeypatch.setattr(_driver, "sleep_async", fake.sleep_async)
    return fake


def raw_path(request: httpx.Request) -> bytes:
    """The request path exactly as sent (percent-encoding intact), without the query."""
    return request.url.raw_path.split(b"?")[0]


def iso(seconds: float) -> str:
    """Unix seconds as the ISO 8601 text the API sends."""
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
