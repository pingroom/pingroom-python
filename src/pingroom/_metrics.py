"""Room metric limits and the checks that mirror the server's ``RoomMetricRules``.

A typo fails here, before a request, instead of as a 422. The server stays
authoritative: it also knows the stored metric (a partial update is judged by
the stored format) and the ISO 4217 code list, so nothing here is stricter than
it. Only the ``[A-Z]{3}`` shape of a currency code is checked, so a code the
server accepts is never refused.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Optional, Tuple

from ._guards import invalid_request, is_number
from ._text import ascii_lower, char_length, js_trim

__all__ = [
    "MAX_METRICS_PER_ROOM",
    "METRIC_FORMATS",
    "METRIC_KEY_PATTERN",
]

#: Metrics one room can hold (``RoomMetric::MAX_PER_ROOM``).
MAX_METRICS_PER_ROOM = 8
#: Every format, in the server's order (``RoomMetric::FORMATS``).
METRIC_FORMATS: Tuple[str, ...] = ("number", "currency", "percent", "unit", "text")
MAX_METRIC_KEY_LENGTH = 40
#: A key as the server stores it: a lowercase letter or digit first, then
#: lowercase letters, digits, ``-`` and ``_``.
METRIC_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MAX_METRIC_LABEL_LENGTH = 24
MAX_METRIC_UNIT_LENGTH = 8
MAX_METRIC_TEXT_VALUE_LENGTH = 24
#: Largest magnitude a numeric value may have.
MAX_METRIC_ABSOLUTE_VALUE = 1e15

_CURRENCY_SHAPE = re.compile(r"[A-Z]{3}")
# A plain decimal, the only text the server reads as a number: no exponent,
# hex, padding, NaN or Infinity.
_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def normalize_metric_key(key: Any, field: str = "key") -> str:
    """The key the server addresses a metric by.

    Trimmed and lowercased (ASCII only, like PHP's ``strtolower``), then checked
    against ``METRIC_KEY_PATTERN``.
    """
    if not isinstance(key, str):
        raise invalid_request(f"`{field}` must be a string.")
    normalized = ascii_lower(js_trim(key))
    if not METRIC_KEY_PATTERN.fullmatch(normalized):
        raise invalid_request(
            f"`{field}` must be 1\N{EN DASH}{MAX_METRIC_KEY_LENGTH} characters: "
            'letters, digits, "-" and "_", starting with a letter or digit.'
        )
    return normalized


def _assert_short_text(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise invalid_request(f"`{field}` must be a string.")
    text = js_trim(value)
    if text == "" or char_length(text) > maximum:
        raise invalid_request(f"`{field}` must be 1\N{EN DASH}{maximum} characters.")
    if _CONTROL.search(text):
        raise invalid_request(f"`{field}` must not contain control characters.")
    return text


def _assert_numeric_value(value: Any, fmt: Optional[str], field: str) -> None:
    label = "a number" if fmt is None else f"a number for the {fmt} format"
    too_large = invalid_request(
        f"`{field}` must have an absolute value of at most {MAX_METRIC_ABSOLUTE_VALUE:.0f}."
    )
    number: float
    try:
        if is_number(value):
            number = float(value)
        elif isinstance(value, str) and _DECIMAL.fullmatch(js_trim(value)):
            number = float(js_trim(value))
        else:
            raise invalid_request(
                f"`{field}` must be {label}: a finite number, "
                'or a plain decimal string such as "12.5".'
            )
    except OverflowError:
        # A Python int beyond the float range: far past the cap.
        raise too_large from None
    if not math.isfinite(number):
        raise invalid_request(f"`{field}` must be a finite number (not NaN or Infinity).")
    if abs(number) > MAX_METRIC_ABSOLUTE_VALUE:
        raise too_large


def _assert_text_value(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise invalid_request(f"`{field}` must be a string for the text format.")
    if char_length(value) > MAX_METRIC_TEXT_VALUE_LENGTH or js_trim(value) == "":
        raise invalid_request(
            f"`{field}` must be 1\N{EN DASH}{MAX_METRIC_TEXT_VALUE_LENGTH} "
            "characters for the text format."
        )
    if _CONTROL.search(value):
        raise invalid_request(f"`{field}` must not contain control characters.")


def assert_metric_fields(
    fields: Mapping[str, Any], *, require_value: bool, prefix: str = ""
) -> None:
    """Check the fields of one metric write.

    The value (required when ``require_value``), and the optional label, format
    and unit. Used by ``metrics.set()`` and by the ``metric`` of an incoming
    webhook, which share the server rule.

    A value with no ``format`` is a number or a string: the server makes a new
    metric ``number`` or ``text`` by its type and judges an existing one by its
    stored format, so only what holds for either is checked.
    """

    def name(field: str) -> str:
        return f"{prefix}{field}"

    value = fields.get("value")
    label = fields.get("label")
    fmt = fields.get("format")
    unit = fields.get("unit")

    if fmt is not None and fmt not in METRIC_FORMATS:
        raise invalid_request(f"`{name('format')}` must be one of: {', '.join(METRIC_FORMATS)}.")
    if label is not None:
        _assert_short_text(label, MAX_METRIC_LABEL_LENGTH, name("label"))

    if unit is not None:
        if fmt is not None and fmt != "currency" and fmt != "unit":
            raise invalid_request(
                f"`{name('unit')}` is only allowed with the currency and unit formats."
            )
        text = _assert_short_text(unit, MAX_METRIC_UNIT_LENGTH, name("unit"))
        if fmt == "currency" and not _CURRENCY_SHAPE.fullmatch(text.upper()):
            raise invalid_request(
                f"`{name('unit')}` for the currency format must be an ISO 4217 code such as USD."
            )

    if value is None:
        if require_value:
            raise invalid_request(f"`{name('value')}` is required.")
        return
    if not is_number(value) and not isinstance(value, str):
        raise invalid_request(f"`{name('value')}` must be a number or a string.")
    if fmt == "text":
        _assert_text_value(value, name("value"))
    elif fmt is not None or is_number(value):
        _assert_numeric_value(value, fmt, name("value"))
    else:
        # No format and a string: text for a new metric, a number for an
        # existing numeric one. Hold it to the limits both would.
        text_value = str(value)
        if (
            char_length(text_value) > MAX_METRIC_TEXT_VALUE_LENGTH
            or js_trim(text_value) == ""
            or _CONTROL.search(text_value)
        ):
            raise invalid_request(
                f"`{name('value')}` must be a number, or a string of "
                f"1\N{EN DASH}{MAX_METRIC_TEXT_VALUE_LENGTH} characters without control characters."
            )
