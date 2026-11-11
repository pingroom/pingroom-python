"""Helpers that build the reserved parts of a Ping's structured ``data``.

``link_ping`` makes a Ping a tappable link. ``location_ping`` attaches a
map-ready point. Spread the result into a Ping's ``data``::

    client.broadcast(
        "ab12cd",
        "Build 512 ready",
        data={"commit": "abc123", **link_ping("https://ci.example.com/b/512", button_label="Open build")},
    )

Both check their input the way the server does, so a bad value fails here
instead of at the API boundary. A bad value raises ``ValueError`` and a value of
the wrong type raises ``TypeError``.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

from ._guards import is_number

__all__ = ["extract_location_ping", "link_ping", "location_ping"]

_URL_MAX = 2048
_LINK_LABEL_MAX = 26
_LOCATION_LABEL_MAX = 100
_LOCATION_ADDRESS_MAX = 255

_MISSING: Any = object()


def link_ping(url: str, *, button_label: Optional[str] = None) -> Dict[str, str]:
    """Build the ``data`` fragment for a link Ping.

    The server convention is two reserved keys inside ``data``: ``url``
    (absolute http(s), at most 2048 characters) and ``button_label`` (at most
    26 characters). Lengths count Unicode characters, as the server does.
    """
    if not isinstance(url, str):
        raise TypeError("link_ping: url must be a string")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        raise ValueError("link_ping: url is not a valid URL") from None
    if scheme == "" or (scheme in ("http", "https") and not host):
        raise ValueError("link_ping: url is not a valid URL")
    if scheme not in ("http", "https"):
        raise ValueError("link_ping: url must be an absolute http(s) URL")
    if len(url) > _URL_MAX:
        raise ValueError(f"link_ping: url must be at most {_URL_MAX} characters")
    out: Dict[str, str] = {"url": url}
    if button_label is not None:
        if not isinstance(button_label, str):
            raise TypeError("link_ping: button_label must be a string")
        if len(button_label) > _LINK_LABEL_MAX:
            raise ValueError(
                f"link_ping: button_label must be at most {_LINK_LABEL_MAX} characters"
            )
        out["button_label"] = button_label
    return out


def _coordinate(value: Any, name: str, low: float, high: float) -> Any:
    if not is_number(value) or not math.isfinite(value):
        raise TypeError(f"location_ping: {name} must be a finite JSON number")
    if value < low or value > high:
        raise ValueError(f"location_ping: {name} must be between {low:g} and {high:g}")
    return value


def _optional_string(candidate: Mapping[str, Any], name: str, maximum: int) -> Any:
    value = candidate.get(name, _MISSING)
    if value is _MISSING:
        return _MISSING
    if not isinstance(value, str):
        raise TypeError(f"location_ping: {name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"location_ping: {name} must be at most {maximum} characters")
    return value


def _validate_location(candidate: Any) -> Dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise TypeError("location_ping: input must be an object")
    location: Dict[str, Any] = {
        "latitude": _coordinate(candidate.get("latitude"), "latitude", -90, 90),
        "longitude": _coordinate(candidate.get("longitude"), "longitude", -180, 180),
    }
    label = _optional_string(candidate, "label", _LOCATION_LABEL_MAX)
    address = _optional_string(candidate, "address", _LOCATION_ADDRESS_MAX)
    if label is not _MISSING:
        location["label"] = label
    if address is not _MISSING:
        location["address"] = address
    return location


def location_ping(
    latitude: float,
    longitude: float,
    *,
    label: Optional[str] = None,
    address: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build and validate the reserved ``data.location`` fragment for a location Ping.

    ``latitude`` is in decimal degrees, -90 to 90 inclusive; ``longitude`` is
    -180 to 180 inclusive. ``label`` is a display label (at most 100 Unicode
    characters) and ``address`` a human-readable address (at most 255).
    """
    candidate: Dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if label is not None:
        candidate["label"] = label
    if address is not None:
        candidate["address"] = address
    return {"location": _validate_location(candidate)}


def extract_location_ping(data: Any) -> Optional[Dict[str, Any]]:
    """Read a valid reserved location from an arbitrary received ``data`` value.

    Invalid or absent location metadata is ignored (``None``) instead of
    raising while a notification feed is being processed.
    """
    if not isinstance(data, Mapping) or "location" not in data:
        return None
    try:
        return _validate_location(data["location"])
    except (TypeError, ValueError):
        return None
