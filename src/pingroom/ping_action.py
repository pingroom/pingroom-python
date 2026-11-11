"""Buttons on a Ping: up to three, on the notification's long-press actions and
on the Ping's card in the app.

They live under the reserved ``data.actions`` key of an agent's
``broadcast()`` or an incoming webhook (``send_incoming_webhook``)::

    ping = client.broadcast(
        "ab12cd",
        "Disk 91% on db-2",
        data={
            "actions": [
                ping_action.open_url("Open dashboard", "https://grafana.example.com/d/disk"),
                ping_action.webhook(
                    "Clean up now",
                    "https://ops.example.com/cleanup",
                    id="clean",
                    style="danger",
                    body={"host": "db-2"},
                ),
            ]
        },
    )
    # Keep ping["action_signing_secret"]: every tap on "Clean up now" reaches
    # https://ops.example.com/cleanup signed with it (verify_webhook_signature).

* ``open_url`` opens an https page. The app shows its host under the label and
  opens it in the in-app browser; the URL never rides the push.
* ``webhook`` never makes the phone call your URL. A tap calls PingRoom, which
  POSTs a signed ``notification.action`` event to it (once per person, for 24
  hours). The URL and ``body`` are never shown to anyone. The room owner must
  be on Pro.

Not in public or personal rooms, not with ``requires_ack``, never on a
question, handoff, approval, live stream, replacement or quick-action press,
and a Ping with buttons cannot be replaced. Each builder checks its button the
way the server does and raises ``PingRoomError`` (``invalid_data``) when the
server would refuse it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ._ping_actions import normalize_ping_actions
from .errors import PingRoomError

__all__ = ["open_url", "webhook"]


_FIELD_PREFIX = re.compile(r"^data\.actions\.0\.?")


def _build(button: Dict[str, Any]) -> Dict[str, Any]:
    actions, errors = normalize_ping_actions([button])
    if errors or len(actions) != 1:
        problems = " ".join(
            f"{_FIELD_PREFIX.sub('', field) or 'button'}: {message}"
            for field, message in errors.items()
        )
        raise PingRoomError(f"ping_action: {problems}", code="invalid_data")
    # The cleaned label is what every recipient sees.
    return {**button, "label": actions[0]["label"]}


def _with_options(
    base: Dict[str, Any],
    id: Optional[str],
    style: Optional[str],
    body: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(base)
    if id is not None:
        out["id"] = id
    if style is not None:
        out["style"] = style
    if body is not None:
        out["body"] = body
    return out


def open_url(
    label: str,
    url: str,
    *,
    id: Optional[str] = None,
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """A button that opens an https link. The app shows the link's host under the label.

    ``id`` is your id for the button (``[a-z0-9_-]{1,24}``, unique on the Ping;
    it defaults to ``a1``, ``a2``, ``a3`` by position). ``style`` is
    ``default``, ``primary`` or ``danger``.
    """
    return _build(_with_options({"label": label, "type": "open_url", "url": url}, id, style, None))


def webhook(
    label: str,
    url: str,
    *,
    id: Optional[str] = None,
    style: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A button whose tap PingRoom delivers to your https URL as a signed
    ``notification.action`` event. The URL takes no port.

    ``body`` is a JSON object (at most 10 keys, 1024 bytes) echoed back to your
    URL as ``body`` on every tap. It is never shown to recipients.
    """
    return _build(_with_options({"label": label, "type": "webhook", "url": url}, id, style, body))
