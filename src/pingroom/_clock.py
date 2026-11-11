"""The clocks the wait loops read.

Kept in one module, and always called as ``_clock.now()`` / ``_clock.monotonic()``,
so tests can replace both with a fake clock and run minutes of backoff instantly.
"""

from __future__ import annotations

import time


def now() -> float:
    """Wall-clock Unix seconds. Compared against the server's ``expires_at``."""
    return time.time()


def monotonic() -> float:
    """A monotonic clock, for intervals between polls."""
    return time.monotonic()
