"""Buttons on a Ping: the reserved ``data.actions`` key.

Checked client-side the way ``App\\Support\\PingActionRules`` checks it on the
server, so a button the server would refuse fails here, before any request,
with the same reason. Pinned by ``docs/contracts/ping-actions.json``
(vendored at ``tests/contracts/ping-actions.json``): the contract test replays
its limits, labels and URLs through these functions.

Unicode categories come from the running interpreter's ``unicodedata``, so a
code point assigned after that interpreter's Unicode version is not classified
until the interpreter is upgraded.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from ._guards import compact_json, invalid_data, server_json_byte_length
from ._reserved_labels import RESERVED_PING_ACTION_LABELS
from .errors import PingRoomError

__all__ = [
    "MAX_PING_ACTIONS",
    "MAX_PING_ACTION_BODY_BYTES",
    "MAX_PING_ACTION_BODY_KEYS",
    "MAX_PING_ACTION_LABEL_LENGTH",
    "MAX_PING_ACTION_URL_LENGTH",
    "PING_ACTION_ID_PATTERN",
    "PING_ACTION_RUN_WINDOW_HOURS",
    "PING_ACTION_STYLES",
    "PING_ACTION_TYPES",
    "RESERVED_PING_ACTION_LABELS",
]

MAX_PING_ACTIONS = 3
PING_ACTION_ID_PATTERN = re.compile(r"[a-z0-9_-]{1,24}")
MAX_PING_ACTION_LABEL_LENGTH = 26
MAX_PING_ACTION_URL_LENGTH = 2048
MAX_PING_ACTION_BODY_BYTES = 1024
MAX_PING_ACTION_BODY_KEYS = 10
#: A button can be run only while its Ping is younger than this.
PING_ACTION_RUN_WINDOW_HOURS = 24
PING_ACTION_TYPES: Tuple[str, ...] = ("open_url", "webhook")
PING_ACTION_STYLES: Tuple[str, ...] = ("default", "primary", "danger")

_ACTION_KEYS: FrozenSet[str] = frozenset({"id", "label", "type", "url", "style", "body"})

# Last labels that never name a public host (server: NON_PUBLIC_TLDS).
_NON_PUBLIC_TLDS: FrozenSet[str] = frozenset(
    {
        "localhost",
        "localdomain",
        "local",
        "internal",
        "lan",
        "intranet",
        "corp",
        "home",
        "arpa",
        "onion",
        "test",
        "invalid",
        "example",
    }
)

_RESERVED: FrozenSet[str] = frozenset(RESERVED_PING_ACTION_LABELS)

_MESSAGE_PROHIBITED = (
    "Buttons (data.actions) can only be sent with an ordinary Ping from an agent "
    "or an incoming webhook."
)

# Format characters a label may keep: the zero-width (non-)joiners, which emoji
# sequences need, and the emoji tag block.
_ALLOWED_FORMAT = frozenset({chr(0x200C), chr(0x200D)})
_TAG_BLOCK = range(0xE0020, 0xE007F + 1)
_REFUSED_CATEGORIES = frozenset({"Cc", "Zl", "Zp", "Co"})
_IDEOGRAPHIC_STOPS = str.maketrans({chr(0x3002): ".", chr(0xFF0E): ".", chr(0xFF61): "."})


class LabelVerdict(NamedTuple):
    label: str
    error: Optional[str]


class UrlVerdict(NamedTuple):
    url: str
    host: str
    error: Optional[str]


def _category(char: str) -> str:
    return unicodedata.category(char)


def has_ping_actions(data: Any) -> bool:
    """Whether ``data`` carries the reserved key at all (``None`` counts as absent)."""
    return isinstance(data, dict) and data.get("actions") is not None


def ping_action_comparison_form(label: str) -> str:
    """The comparison form of a label.

    Compatibility-folded, lowercased, letters, digits and combining marks only:
    ``"<emoji> Pong Back"`` and ``"PONG back"`` are both ``pongback``.
    """
    folded = unicodedata.normalize("NFKC", label).lower()
    return "".join(char for char in folded if _category(char)[0] in ("L", "N", "M"))


# The folded label is mapped to one symbol per character so the shapes the
# server's PHP patterns match can be matched with plain `re`, which has no
# Unicode property classes. ASCII letters and digits stay as they are;
# `L` is a non-ASCII letter, `D` a non-ASCII decimal digit, `O` any other
# number (letter-number, other-number), and `#` everything else.
_LN = "a-z0-9LDO"
_ADDRESS_IPV4 = re.compile(r"(?<![" + _LN + r"])[0-9D]{1,3}(?:\.[0-9D]{1,3}){3}(?![" + _LN + r"])")
_LABEL = r"[" + _LN + r"](?:[" + _LN + r"-]*[" + _LN + r"])?"
_ADDRESS_DOMAIN = re.compile(
    r"(?<!["
    + _LN
    + r"_-])"
    + _LABEL
    + r"(?:\."
    + _LABEL
    + r")*\.(?:[a-zL]{2,}|xn--[a-z0-9-]+)(?!["
    + _LN
    + r"])"
)


def _symbols(text: str) -> str:
    out: List[str] = []
    for char in text:
        if char.isascii():
            out.append(char)
            continue
        category = _category(char)
        if category[0] == "L":
            out.append("L")
        elif category == "Nd":
            out.append("D")
        elif category[0] == "N":
            out.append("O")
        else:
            out.append("#")
    return "".join(out)


def ping_action_label_looks_like_address(label: str) -> bool:
    """Whether a label holds anything shaped like a destination.

    A scheme, ``www.``, a dotted IPv4 address, or ``name.tld`` with an
    alphabetic or punycode TLD, after compatibility folding and with the
    ideographic full stops browsers treat as dots. Every decimal digit counts
    as a digit, as PHP's ``/u`` patterns count them.
    """
    stripped = "".join(char for char in label if _category(char) != "Cf")
    folded = unicodedata.normalize("NFKC", stripped).lower().translate(_IDEOGRAPHIC_STOPS)
    if "://" in folded or "www." in folded:
        return True
    symbols = _symbols(folded)
    return bool(_ADDRESS_IPV4.search(symbols) or _ADDRESS_DOMAIN.search(symbols))


def clean_ping_action_label(label: Any) -> LabelVerdict:
    """Clean one label the way the server does.

    NFC; refuse every control, private-use, lone-surrogate and invisible
    formatting character except the zero-width (non-)joiners and the emoji tag
    block; collapse spaces; trim; then refuse an empty, overlong,
    address-shaped or reserved label.
    """
    if not isinstance(label, str):
        return LabelVerdict("", "A button needs a label.")
    if any(_category(char) == "Cs" for char in label):
        # A lone surrogate is not valid text: the server cannot decode it at all.
        return LabelVerdict("", "A button needs a label.")
    cleaned = unicodedata.normalize("NFC", label)
    for char in cleaned:
        category = _category(char)
        if category in _REFUSED_CATEGORIES or (
            category == "Cf" and char not in _ALLOWED_FORMAT and ord(char) not in _TAG_BLOCK
        ):
            return LabelVerdict(
                "",
                "A button label may not contain line breaks, control characters "
                "or invisible formatting characters.",
            )
    # Runs of space separators become one space, then the ends are trimmed.
    collapsed: List[str] = []
    for char in cleaned:
        if _category(char) == "Zs":
            if not collapsed or collapsed[-1] != " ":
                collapsed.append(" ")
        else:
            collapsed.append(char)
    cleaned = "".join(collapsed).strip(" ")
    if cleaned == "":
        return LabelVerdict("", "A button needs a label.")
    if len(cleaned) > MAX_PING_ACTION_LABEL_LENGTH:
        return LabelVerdict(
            "", f"A button label is at most {MAX_PING_ACTION_LABEL_LENGTH} characters."
        )
    if ping_action_label_looks_like_address(cleaned):
        return LabelVerdict(
            "",
            "A button label may not contain a web address, domain name or IP address. "
            "The app shows where the button goes.",
        )
    if ping_action_comparison_form(cleaned) in _RESERVED:
        return LabelVerdict("", "That label is one of PingRoom's own buttons. Choose another.")
    return LabelVerdict(cleaned, None)


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOST_TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
_AUTHORITY = re.compile(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?")
_PRINTABLE_NO_SPACE = re.compile(r"[\x21-\x7e]+")


def is_public_ping_action_hostname(host: str) -> bool:
    """A lowercase ASCII hostname of at least two labels.

    Its last label is alphabetic (or punycode) and not a special-use name.
    """
    if host == "" or len(host) > 253 or host.endswith("."):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not _HOST_LABEL.fullmatch(label):
            return False
    tld = labels[-1]
    return bool(_HOST_TLD.fullmatch(tld)) and tld not in _NON_PUBLIC_TLDS


def ping_action_url_verdict(url: Any, action_type: str) -> UrlVerdict:
    """The URL rules for both button types, and the host the server derives.

    ``https://``, printable ASCII only, a bare hostname as the authority (and
    a port for a link only), a public hostname. An internationalized domain
    must arrive as punycode. The server checks a webhook target again when it
    delivers (DNS, private addresses), which this cannot.
    """

    def refuse(error: str) -> UrlVerdict:
        return UrlVerdict("", "", error)

    if not isinstance(url, str) or url == "":
        return refuse("A button needs a url.")
    if len(url.encode("utf-8", "surrogatepass")) > MAX_PING_ACTION_URL_LENGTH:
        return refuse(f"A button url is at most {MAX_PING_ACTION_URL_LENGTH} characters.")
    if not url.startswith("https://"):
        return refuse("A button url must start with https://.")
    if not _PRINTABLE_NO_SPACE.fullmatch(url) or "\\" in url:
        return refuse(
            "A button url may contain only printable ASCII characters: percent-encode the rest, "
            "and give an internationalized domain in its punycode (xn--) form."
        )

    rest = url[len("https://") :]
    end = re.search(r"[/?#]", rest)
    authority = rest if end is None else rest[: end.start()]
    match = _AUTHORITY.fullmatch(authority)
    if match is None:
        return refuse(
            "A button url needs a plain hostname: no user name or password, "
            "no IP address in brackets."
        )
    host = match.group(1).lower()
    port = match.group(2)
    if port is not None:
        if action_type == "webhook":
            return refuse(
                "A webhook button url cannot name a port: deliveries go to the standard HTTPS port."
            )
        if int(port) < 1 or int(port) > 65535:
            return refuse("A button url has an invalid port.")
    if not is_public_ping_action_hostname(host):
        return refuse(
            "A button url needs a public hostname such as example.com: "
            "no IP address, localhost or internal name."
        )

    # A second reading must name the same host, as the server's parse_url does.
    try:
        parsed = urlsplit(url)
        same_host = parsed.hostname == host
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError:
        return refuse("A button url must be a valid https URL.")
    if not same_host or has_credentials:
        return refuse("A button url must be a valid https URL.")
    return UrlVerdict(url, host, None)


def _is_php_list(value: Dict[Any, Any]) -> bool:
    """PHP decodes an object keyed ``"0"``, ``"1"``, ... in order as a list."""
    keys = [str(key) for key in value]
    return len(keys) > 0 and all(key == str(index) for index, key in enumerate(keys))


def ping_action_body_problem(body: Any) -> Optional[str]:
    """Why a webhook button's ``body`` is refused, or ``None`` when it is fine.

    A JSON object of at most 10 top-level keys and 1024 bytes as PHP's escaped
    ``json_encode`` measures it. An object whose keys are exactly ``"0"``,
    ``"1"``, ... is a list to the server.
    """
    if not isinstance(body, dict) or _is_php_list(body):
        return "A webhook button body must be a JSON object."
    if len(body) > MAX_PING_ACTION_BODY_KEYS:
        return f"A webhook button body takes at most {MAX_PING_ACTION_BODY_KEYS} keys."
    try:
        encoded = compact_json(body)
    except (TypeError, ValueError, RecursionError):
        return "A webhook button body must be valid UTF-8 JSON."
    if server_json_byte_length(encoded) > MAX_PING_ACTION_BODY_BYTES:
        return f"A webhook button body must not exceed {MAX_PING_ACTION_BODY_BYTES} bytes."
    return None


def normalize_ping_actions(actions: Any) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Validate and clean a ``data.actions`` value as the server does.

    Returns ``(actions, errors)``. ``errors`` maps a field
    (``data.actions.1.label``) to its refusal and is empty only when every
    button is valid; ``actions`` is then the normalised list the server stores
    (``id`` defaulted to ``a1``..``a3``, the cleaned label, the derived host).
    """
    if not isinstance(actions, list):
        return [], {"data.actions": "Buttons (data.actions) must be a list."}
    if len(actions) == 0 or len(actions) > MAX_PING_ACTIONS:
        return [], {"data.actions": f"A Ping takes 1 to {MAX_PING_ACTIONS} buttons."}

    errors: Dict[str, str] = {}
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for index, action in enumerate(actions):
        field = f"data.actions.{index}"
        before = len(errors)

        if not isinstance(action, dict) or (len(action) > 0 and _is_php_list(action)):
            errors[field] = "Each button must be an object."
            continue
        unknown = [str(key) for key in action if key not in _ACTION_KEYS]
        if unknown:
            errors[field] = f"Unknown button field: {', '.join(unknown)}."
            continue

        action_id = action.get("id")
        if action_id is None:
            action_id = f"a{index + 1}"
        if not isinstance(action_id, str) or not PING_ACTION_ID_PATTERN.fullmatch(action_id):
            errors[f"{field}.id"] = (
                'A button id is 1 to 24 characters: lowercase letters, digits, "-" and "_".'
            )
        elif action_id in seen:
            errors[f"{field}.id"] = "Each button on a Ping needs its own id."
        else:
            seen.add(action_id)

        action_type = action.get("type")
        if not isinstance(action_type, str) or action_type not in PING_ACTION_TYPES:
            errors[f"{field}.type"] = "A button type is open_url or webhook."
            action_type = None

        style = action.get("style")
        if style is None:
            style = "default"
        if not isinstance(style, str) or style not in PING_ACTION_STYLES:
            errors[f"{field}.style"] = "A button style is default, primary or danger."

        label = clean_ping_action_label(action.get("label"))
        if label.error is not None:
            errors[f"{field}.label"] = label.error

        url = ping_action_url_verdict(
            action.get("url"), action_type if isinstance(action_type, str) else "open_url"
        )
        if url.error is not None:
            errors[f"{field}.url"] = url.error

        body = action.get("body")
        if body is not None:
            problem = (
                ping_action_body_problem(body)
                if action_type == "webhook"
                else "Only a webhook button takes a body."
            )
            if problem is not None:
                errors[f"{field}.body"] = problem

        if len(errors) != before:
            continue

        normalized.append(
            {
                "id": action_id,
                "position": index,
                "type": action_type,
                "label": label.label,
                "style": style,
                "url": url.url,
                "host": url.host,
                "body": body if isinstance(body, dict) and len(body) > 0 else None,
            }
        )

    if errors:
        return [], errors
    return normalized, {}


def assert_ping_actions_data(data: Any) -> None:
    """Refuse a ``data`` whose buttons the server would refuse, naming every field.

    A no-op without ``data.actions``.
    """
    if not has_ping_actions(data):
        return
    _, errors = normalize_ping_actions(data["actions"])
    if errors:
        details = " ".join(f"{field}: {message}" for field, message in errors.items())
        raise invalid_data(f"Invalid buttons: {details}")


def refuse_ping_actions_data(data: Any, where: str) -> None:
    """Refuse ``data.actions`` on a route that never carries buttons.

    A Question, a handoff, an approval, a live-progress stream, a replacement
    or a quick-action press. The server answers 422 there; this says so before
    the request.
    """
    if not has_ping_actions(data):
        return
    raise PingRoomError(f"{where} cannot carry buttons. {_MESSAGE_PROHIBITED}", code="invalid_data")
