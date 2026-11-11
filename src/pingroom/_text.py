"""Small text helpers that reproduce the exact trimming and counting rules the
server and the TypeScript SDK use, so a client-side check never disagrees with
the route behind it."""

from __future__ import annotations

import re

# JavaScript's String.prototype.trim() set: WhiteSpace plus LineTerminator.
# Python's str.strip() differs (it also strips \x1c-\x1f and \x85, and not U+FEFF).
# Written as code points so no invisible character sits in the source.
JS_WHITESPACE = "".join(
    chr(code)
    for code in (
        0x0009,
        0x000A,
        0x000B,
        0x000C,
        0x000D,
        0x0020,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)

# What the server strips from a tag before checking it: JavaScript's whitespace
# plus the zero-width space. A superset of the server's set, so never stricter.
TAG_TRIM = JS_WHITESPACE + chr(0x200B)

# PHP trim()'s default set: what the server strips from both ends of a key.
PHP_TRIM = " \t\n\r\x00\x0b"


def js_trim(value: str) -> str:
    """Trim the way JavaScript's ``String.prototype.trim`` does."""
    return value.strip(JS_WHITESPACE)


def php_trim(value: str) -> str:
    """Trim the way PHP's ``trim()`` does with its default character list."""
    return value.strip(PHP_TRIM)


_ASCII_UPPER = re.compile(r"[A-Z]")


def ascii_lower(value: str) -> str:
    """Lowercase ASCII letters only, like PHP 8's ``strtolower``.

    A non-ASCII letter never folds into an ASCII one (a Turkish dotted capital
    I would under ``str.lower()``).
    """
    return _ASCII_UPPER.sub(lambda match: match.group(0).lower(), value)


def char_length(value: str) -> int:
    """Count Unicode code points, as Laravel's ``mb_strlen`` does."""
    return len(value)
