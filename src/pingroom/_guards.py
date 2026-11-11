"""Client-side checks that mirror the server's rules.

Each check is the same one the TypeScript SDK makes, and none is stricter than
the route behind it: a request the server would accept must never be refused
here. The server stays authoritative.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, List, Optional, Set

from ._text import TAG_TRIM, ascii_lower, char_length, js_trim, php_trim
from .errors import PingRoomError

# Structured `data`: the server's limits.
MAX_DATA_BYTES = 8 * 1024
MAX_DATA_KEYS = 25
MAX_PRIVATE_PING_MESSAGE_LENGTH = 120
MAX_PUBLIC_PING_MESSAGE_LENGTH = 160
MAX_PING_TITLE_LENGTH = 40

# `App\Support\PingTagRules`: at most 10 tags, each 1-32 characters.
MAX_PING_TAGS = 10
MAX_PING_TAG_LENGTH = 32
PING_TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

# `App\Support\PingReplacement`: a replace_key is 1-64 characters of letters,
# digits and `. _ : -`.
MAX_REPLACE_KEY_LENGTH = 64
REPLACE_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")

MAX_IDEMPOTENCY_KEY_LENGTH = 255
_PRINTABLE_ASCII = re.compile(r"[\x20-\x7e]+")


def invalid_request(message: str) -> PingRoomError:
    return PingRoomError(message, code="invalid_request")


def invalid_data(message: str) -> PingRoomError:
    return PingRoomError(message, code="invalid_data")


def is_number(value: Any) -> bool:
    """A JSON number: an int or a float, never a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def assert_max_length(value: Any, maximum: int, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise invalid_request(f"`{field}` must be a string.")
    length = char_length(value)
    if length > maximum:
        raise invalid_request(f"`{field}` may have at most {maximum} characters (got {length}).")


def server_json_byte_length(encoded: str) -> int:
    """Bytes a JSON string takes as PHP's default ``json_encode()`` writes it.

    That is what the server's 8 KB cap measures: every non-ASCII UTF-16 unit
    becomes a six-byte ``\\uXXXX`` escape (a character outside the BMP is two
    units, so twelve bytes) and ``/`` becomes ``\\/``. Non-Latin text is several
    times larger there than its UTF-8 size.
    """
    total = 0
    for char in encoded:
        code = ord(char)
        if code > 0xFFFF:
            total += 12
        elif code > 0x7F:
            total += 6
        elif code == 0x2F:
            total += 2
        else:
            total += 1
    return total


def compact_json(value: Any) -> str:
    """JSON the way ``JSON.stringify`` writes it: no spaces, non-ASCII kept as is.

    Raises ``ValueError`` or ``TypeError`` when the value is not JSON (a NaN, a
    circular structure, an object with no JSON form).
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def assert_structured_data(data: Any) -> None:
    if data is None:
        return
    if not isinstance(data, dict):
        raise invalid_data("`data` must be a JSON object (not an array or scalar).")
    if len(data) > MAX_DATA_KEYS:
        raise invalid_data(f"`data` may have at most {MAX_DATA_KEYS} keys (got {len(data)}).")
    try:
        encoded = compact_json(data)
    except (TypeError, ValueError, RecursionError):
        raise invalid_data("`data` must be JSON-serializable.") from None
    size = server_json_byte_length(encoded)
    if size > MAX_DATA_BYTES:
        raise invalid_data(f"`data` is too large ({size} bytes, max {MAX_DATA_BYTES}).")


def normalize_tag(tag: str) -> str:
    """What the server does to a tag before it checks it.

    Trim, then lowercase ASCII only. The trim set is a superset of the
    server's, so this is never stricter than the route behind it.
    """
    return ascii_lower(tag.strip(TAG_TRIM))


def _tag_shape() -> str:
    return (
        f"a tag is 1 to {MAX_PING_TAG_LENGTH} characters: lowercase letters, digits, "
        '"-" and "_", starting with a letter or digit'
    )


def _assert_tag_text(tag: str, field: str) -> None:
    if not PING_TAG_PATTERN.fullmatch(tag):
        shown = f"{tag[:40]}…" if len(tag) > 40 else tag
        raise invalid_request(
            f"`{field}` {json.dumps(shown, ensure_ascii=False)} is not a valid tag: {_tag_shape()}."
        )


def assert_tags(tags: Any, field: str = "tags") -> None:
    """Check a ping's ``tags`` the way the server does.

    Trimmed, lowercased, blanks dropped and duplicates removed first, then at
    most 10, each matching the tag rule. The payload is sent as given: the
    server normalises it the same way, so ``["Deploy ", "deploy"]`` is one tag.
    """
    if tags is None:
        return
    if not isinstance(tags, (list, tuple)) or any(not isinstance(tag, str) for tag in tags):
        raise invalid_request(f"`{field}` must be an array of strings.")
    seen: Set[str] = set()
    normalized: List[str] = []
    for tag in tags:
        cleaned = normalize_tag(tag)
        if cleaned != "" and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    if len(normalized) > MAX_PING_TAGS:
        raise invalid_request(
            f"`{field}` may have at most {MAX_PING_TAGS} tags (got {len(normalized)})."
        )
    for tag in normalized:
        _assert_tag_text(tag, field)


def assert_tag(tag: Any, field: str = "tag") -> None:
    """Check the single ``tag`` a read filters by, with the rule of one ``tags`` entry."""
    if tag is None:
        return
    if not isinstance(tag, str):
        raise invalid_request(f"`{field}` must be a string.")
    _assert_tag_text(normalize_tag(tag), field)


def assert_replace_key(value: Any, field: str = "replace_key") -> None:
    """Check a ``replace_key`` the way the server does.

    The key is sent as given (the server does not trim it), so a key with a
    space or any other character outside the set is refused here rather than
    as a 422.
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise invalid_request(f"`{field}` must be a string.")
    if (
        len(value) < 1
        or len(value) > MAX_REPLACE_KEY_LENGTH
        or not REPLACE_KEY_PATTERN.fullmatch(value)
    ):
        raise invalid_request(
            f"`{field}` must be 1–{MAX_REPLACE_KEY_LENGTH} characters: "
            'letters, digits, ".", "_", ":" and "-".'
        )


def assert_action_number(value: Any) -> Optional[int]:
    """An action slot is an integer 1-16. Returns it as an ``int`` (``3.0`` becomes ``3``)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_request("action_number must be an integer 1–16.")
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise invalid_request("action_number must be an integer 1–16.")
        value = int(value)
    if value < 1 or value > 16:
        raise invalid_request("action_number must be an integer 1–16.")
    return int(value)


def normalize_idempotency_key(value: Any, field: str = "idempotency_key") -> str:
    """Normalise an idempotency key the way the server reads it.

    Trimmed (``RequestIdempotency::keyFrom``), then 1-255 characters. It
    travels as the ``Idempotency-Key`` header, so it must also be printable
    ASCII (space included): a character outside Latin-1 cannot be sent as a
    header, and a Latin-1 byte above 0x7E reaches the server as invalid UTF-8.
    """
    if not isinstance(value, str):
        raise invalid_request(f"`{field}` must be a string.")
    key = php_trim(value)
    if len(key) < 1 or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or not _PRINTABLE_ASCII.fullmatch(key):
        raise invalid_request(
            f"`{field}` must be 1–{MAX_IDEMPOTENCY_KEY_LENGTH} printable ASCII characters "
            "(letters, digits, punctuation or spaces)."
        )
    return key


def require_non_empty(value: Any, field: str) -> None:
    if not isinstance(value, str) or js_trim(value) == "":
        raise invalid_request(f"`{field}` is required.")
