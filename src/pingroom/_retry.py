"""Long-poll timing, transient-failure retries and the shared wait loop.

Every constant, and the shape of the loop, is the TypeScript SDK's, so the two
back off, space their polls and give up identically.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Generator, Optional, TypeVar

from . import _clock
from ._core import Command, Sleep
from .errors import PingRoomCancelledError, PingRoomError

T = TypeVar("T")
Op = Generator[Command, Any, T]

#: Extra wall-clock allowance over the server's hold window for long-poll calls.
LONG_POLL_BUFFER_SECONDS = 10.0
DEFAULT_WAIT_SECONDS = 20

#: Minimum spacing between the starts of two handoff-wait polls. The route is
#: throttled at 30 requests a minute per human; a short or zero ``timeout``
#: would otherwise let one loop exhaust it.
MIN_HANDOFF_POLL_INTERVAL = 2.1
#: Minimum spacing between the starts of two ``listen()`` polls after an empty
#: answer. ``notifications/wait`` is throttled at 30 requests a minute per
#: human, shared with every other listener on the account.
MIN_LISTEN_POLL_INTERVAL = MIN_HANDOFF_POLL_INTERVAL
#: Minimum spacing between the starts of two approval or question wait polls.
MIN_WAIT_POLL_INTERVAL = MIN_HANDOFF_POLL_INTERVAL

#: First retry delay after a transient failure, in seconds.
TRANSIENT_BACKOFF_BASE = 1.0
#: The smallest backoff base, whatever a caller asks for. A persistent failure
#: would otherwise retry in a tight loop (a base of 0 never grows at all)
#: against wait routes limited to 30 requests a minute each.
MIN_TRANSIENT_BACKOFF = 0.25
#: Ceiling on the exponential backoff (a server Retry-After may ask for longer).
TRANSIENT_BACKOFF_CAP = 30.0
#: No delay may exceed this (the longest timer the TypeScript SDK can set).
MAX_DELAY = 2_147_483.647
#: How long past the last-seen ``expires_at`` a wait keeps retrying an
#: unreachable API. The server reports ``expired`` once expires_at passes, so
#: after this the only outcome left is one that cannot be read. Covers clock skew.
WAIT_EXPIRY_GRACE = 5 * 60.0
#: The bound on an unbroken outage while no response has carried ``expires_at``.
WAIT_UNKNOWN_EXPIRY_OUTAGE = 15 * 60.0


def wait_timeout(seconds: Optional[float]) -> float:
    """The request timeout of a long-poll call: its hold time plus a buffer."""
    return (DEFAULT_WAIT_SECONDS if seconds is None else seconds) + LONG_POLL_BUFFER_SECONDS


def is_cancelled(cancel: Any) -> bool:
    return cancel is not None and bool(cancel.is_set())


def throw_if_cancelled(cancel: Any) -> None:
    if is_cancelled(cancel):
        raise PingRoomCancelledError()


def is_transient_error(error: BaseException) -> bool:
    """A failure a read loop may retry.

    A dropped connection, a request deadline, a 429 (each wait route has its
    own 30/min bucket per human), or a 5xx. Everything else (401/403/404/422, a
    refused redirect, a malformed response) is permanent and must surface.
    """
    return isinstance(error, PingRoomError) and (
        error.code == "network_error"
        or error.code == "timeout"
        or error.status == 429
        or error.status >= 500
    )


def transient_delay(
    error: PingRoomError, failures: int, base: float = TRANSIENT_BACKOFF_BASE
) -> float:
    """Delay before retrying after the ``failures``-th consecutive transient failure.

    Exponential from ``base``, capped at 30 s. A server ``retry_after`` replaces
    the exponential step, but is never waited for less than ``base``. A ``base``
    below ``MIN_TRANSIENT_BACKOFF`` is raised to it.
    """
    requested = base if math.isfinite(base) and base >= 0 else TRANSIENT_BACKOFF_BASE
    floor = max(MIN_TRANSIENT_BACKOFF, requested)
    retry_after = error.retry_after
    if (
        isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and math.isfinite(retry_after)
        and retry_after >= 0
    ):
        return min(MAX_DELAY, max(float(retry_after), floor))
    # Enough doublings to pass the cap from any base; also keeps 2 ** step finite.
    step = min(max(0, failures - 1), 30)
    return min(MAX_DELAY, max(floor, TRANSIENT_BACKOFF_CAP), floor * 2.0**step)


_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]+))?)?)?\s*"
    r"(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?",
    re.IGNORECASE,
)


def time_of(value: Any) -> Optional[float]:
    """An ISO 8601 timestamp as Unix seconds, or ``None`` when it is not one.

    A timestamp with no offset is read as UTC.
    """
    if not isinstance(value, str):
        return None
    match = _ISO.fullmatch(value.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        moment = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    seconds = moment.timestamp() + (float("0." + fraction) if fraction else 0.0)
    if offset and offset.upper() != "Z":
        sign = -1 if offset[0] == "-" else 1
        digits = offset[1:].replace(":", "")
        delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        seconds -= sign * delta.total_seconds()
    return seconds


def _iso(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _unavailable(last: PingRoomError, since: float, what: str) -> PingRoomError:
    reason = f"HTTP {last.status}" if last.status > 0 else (last.code or "network error")
    error = PingRoomError(
        f"PingRoom unavailable since {_iso(since)} ({reason}); stopped waiting for {what}.",
        code="unavailable",
    )
    error.__cause__ = last
    return error


def poll_until_settled(
    *,
    poll: Callable[[], Op[Any]],
    validate: Callable[[Any], T],
    settled: Callable[[T], bool],
    what: str,
    cancel: Any = None,
    min_interval: float = 0.0,
    expires_at: Optional[Callable[[T], Any]] = None,
    on_retry: Optional[Callable[[PingRoomError, int], None]] = None,
) -> Op[T]:
    """The shared human-in-the-loop wait loop.

    Polls until a validated response is terminal; retries transient failures
    with backoff; stops with ``PingRoomCancelledError`` once ``cancel`` is set.
    A response that fails validation is never returned as an outcome.

    The retries are bounded even without a cancel signal. Once only failures
    have come back since the last valid response, and the last-seen
    ``expires_at`` plus ``WAIT_EXPIRY_GRACE`` has passed (or, before any
    ``expires_at`` was seen, after ``WAIT_UNKNOWN_EXPIRY_OUTAGE`` of unbroken
    failures), the wait raises ``unavailable``, the last failure as its cause.
    """
    failures = 0
    failing_since_wall: Optional[float] = None
    failing_since_mono: Optional[float] = None
    # expires_at plus grace (wall clock), from the last valid response that carried one.
    deadline: Optional[float] = None
    while True:
        throw_if_cancelled(cancel)
        started = _clock.monotonic()
        try:
            value = validate((yield from poll()))
        except Exception as error:
            throw_if_cancelled(cancel)
            if not isinstance(error, PingRoomError) or not is_transient_error(error):
                raise
            failures += 1
            if failing_since_wall is None or failing_since_mono is None:
                failing_since_wall = _clock.now()
                failing_since_mono = _clock.monotonic()
            if deadline is not None:
                remaining = deadline - _clock.now()
            else:
                remaining = failing_since_mono + WAIT_UNKNOWN_EXPIRY_OUTAGE - _clock.monotonic()
            if remaining <= 0:
                raise _unavailable(error, failing_since_wall, what) from error
            if on_retry is not None:
                on_retry(error, failures)
            # A fast failure still consumes a poll. Keep retries spaced even
            # when a pending response between failures resets the backoff.
            delay = max(
                transient_delay(error, failures),
                min_interval - (_clock.monotonic() - started),
            )
            # Do not retry earlier than the interval or Retry-After just to fit
            # the outage bound. Wait out the budget, then stop without another
            # request.
            if delay > remaining:
                yield Sleep(remaining, cancel)
                throw_if_cancelled(cancel)
                raise _unavailable(error, failing_since_wall, what) from error
            yield Sleep(delay, cancel)
            continue
        failures = 0
        failing_since_wall = None
        failing_since_mono = None
        expiry = time_of(expires_at(value)) if expires_at is not None else None
        if expiry is not None:
            deadline = expiry + WAIT_EXPIRY_GRACE
        if settled(value):
            return value
        # A transport that answers pending polls at once must not starve
        # timer-driven cancellation: yield between polls, and keep them a floor
        # apart.
        yield Sleep(max(0.0, min_interval - (_clock.monotonic() - started)), cancel)
