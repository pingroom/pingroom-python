"""The request-building and response-reading core shared by every client.

Nothing here performs I/O. The synchronous and asynchronous clients both build
their requests with ``prepare()`` and read the answers with ``interpret()``, so
the wire format (method, path, query, headers, JSON body) and the error mapping
exist once and cannot drift between them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import quote, urlsplit

from ._guards import compact_json, invalid_request
from ._text import js_trim
from .errors import PingRoomError

DEFAULT_BASE_URL = "https://api.pingroom.io"
DEFAULT_TIMEOUT = 30.0


class _Unset:
    """The type of ``UNSET``: "no value was given", distinct from ``None`` (JSON ``null``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False

    def __reduce__(self) -> str:
        return "UNSET"


#: Marks an argument that was left out. ``None`` is a value (it sends JSON ``null``).
UNSET: Any = _Unset()

_ABSOLUTE_PATH = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_FORM_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._")
_URI_COMPONENT_SAFE = "!~*'()"


class Secret:
    """Holds a credential so that no rendering of its owner can print it.

    ``repr()``, ``str()`` and ``format()`` all give ``[redacted]``, and the
    value cannot be pickled or copied by accident. Read it with ``reveal()``.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "[redacted]"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "[redacted]"

    def __reduce__(self) -> Any:
        raise TypeError("a credential cannot be pickled or copied")

    def __bool__(self) -> bool:
        return bool(self._value)


# ---------------------------------------------------------------------------
# Commands: what an operation asks its driver to do.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Multipart:
    """One file part of a multipart upload."""

    name: str
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class Request:
    """One HTTP request an operation wants sent. The driver resumes it with the result."""

    method: str
    path: str
    query: Tuple[Tuple[str, str], ...] = ()
    body: Any = UNSET
    multipart: Optional[Multipart] = None
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Send the Authorization header (default true).
    auth: bool = True
    #: Per-call timeout override, in seconds.
    timeout: Optional[float] = None
    #: ``json`` returns the parsed body; ``raw`` returns the bytes and their type.
    mode: str = "json"
    #: Set by the caller to stop an in-flight call (asynchronous clients only).
    cancel: Any = None


@dataclass(frozen=True)
class Sleep:
    """Wait ``seconds``, or less when ``cancel`` is set meanwhile."""

    seconds: float
    cancel: Any = None


@dataclass(frozen=True)
class Emit:
    """Hand one item to the consumer of an iterating operation."""

    item: Any


Command = Union[Request, Sleep, Emit]


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


def is_loopback_host(hostname: str) -> bool:
    host = hostname.lower()
    if host.startswith("["):
        host = host[1:]
    if host.endswith("]"):
        host = host[:-1]
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _host_display(hostname: str, port: Optional[int]) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return host if port is None else f"{host}:{port}"


def assert_secure_url(raw_url: str, allow_insecure: bool) -> Tuple[str, str, str]:
    """Refuse to carry a credential over plain ``http`` to a non-loopback host.

    ``https`` always passes; ``http`` only to a loopback development host, or
    anywhere when ``allow_insecure`` is set. Returns ``(origin, path, hostname)``.
    The value is never echoed in an error: an incoming-webhook URL carries its
    secret in the path, and error messages end up in logs.
    """
    withheld = PingRoomError(
        "Invalid URL (value withheld because it may contain a secret).", code="invalid_url"
    )
    if not isinstance(raw_url, str):
        raise withheld
    text = raw_url.strip()
    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        hostname = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise withheld from None
    if scheme == "" or not text.lower().startswith(scheme + ":"):
        raise withheld
    if scheme in ("http", "https") and hostname == "":
        raise withheld
    if scheme == "https" or (scheme == "http" and (allow_insecure or is_loopback_host(hostname))):
        default_port = 443 if scheme == "https" else 80
        shown_port = None if port in (None, default_port) else port
        return f"{scheme}://{_host_display(hostname, shown_port)}", parts.path or "/", hostname
    raise PingRoomError(
        f"Refusing to send credentials over an insecure URL "
        f"({scheme}://{_host_display(hostname, port)}). "
        "Use https, or pass allow_insecure=True for trusted local development.",
        code="insecure_url",
    )


@dataclass
class ClientConfig:
    """What a client needs to build a request. The token lives in a ``Secret``."""

    base_url: str
    timeout: float
    user_agent: str
    token: Optional[Secret] = None

    def set_token(self, token: Optional[str]) -> None:
        self.token = Secret(token) if token else None


def normalize_base_url(raw: str, allow_insecure: bool) -> str:
    """Keep the origin and any path prefix; drop a trailing slash, query and fragment."""
    origin, path, _ = assert_secure_url(raw, allow_insecure)
    full = origin + path
    return full[:-1] if full.endswith("/") else full


# ---------------------------------------------------------------------------
# Building a request
# ---------------------------------------------------------------------------


def encode_path_segment(segment: str) -> str:
    """Percent-encode one path segment as ``encodeURIComponent`` does.

    A dot segment would be resolved by URL parsing (``live.get("AB12", "..")``
    would request the room itself), and no id is empty or a dot segment, so
    those are refused.
    """
    if not isinstance(segment, str) or segment in ("", ".", ".."):
        raise invalid_request("A path parameter may not be empty, `.` or `..`.")
    return quote(segment, safe=_URI_COMPONENT_SAFE, errors="surrogatepass")


def _form_quote(text: str) -> str:
    """Encode one query component as ``URLSearchParams`` does (``application/x-www-form-urlencoded``)."""
    out: List[str] = []
    for byte in text.encode("utf-8", "surrogatepass"):
        if byte in _FORM_SAFE:
            out.append(chr(byte))
        elif byte == 0x20:
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def build_url(base_url: str, path: str, query: Tuple[Tuple[str, str], ...]) -> str:
    # Every call is relative to the base URL the token was configured for. An
    # absolute URL would carry the Bearer token to another origin unchecked.
    if _ABSOLUTE_PATH.match(path):
        raise invalid_request("Request paths must be relative to the API base URL.")
    url = base_url + (path if path.startswith("/") else "/" + path)
    if query:
        url += "?" + "&".join(f"{_form_quote(k)}={_form_quote(v)}" for k, v in query)
    return url


@dataclass
class Prepared:
    """A request ready for ``httpx``: everything decided, nothing sent."""

    method: str
    url: str
    headers: Dict[str, str]
    content: Optional[bytes]
    files: Optional[Dict[str, Tuple[str, bytes, str]]]
    timeout: float
    mode: str


def encode_json(value: Any) -> bytes:
    """The JSON request body, UTF-8, the way ``JSON.stringify`` writes it."""
    try:
        return compact_json(value).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        raise invalid_request("The request body must be JSON-serializable.") from None


def prepare(config: ClientConfig, request: Request) -> Prepared:
    url = build_url(config.base_url, request.path, request.query)

    headers: Dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": config.user_agent,
        **request.headers,
    }
    if request.mode == "raw":
        headers = {**headers, "Accept": "*/*", **request.headers}

    if request.auth:
        if config.token is None:
            raise PingRoomError(
                "This call requires an agent token. Pass `token` to the client or call set_token().",
                code="no_token",
            )
        headers["Authorization"] = f"Bearer {config.token.reveal()}"

    content: Optional[bytes] = None
    files: Optional[Dict[str, Tuple[str, bytes, str]]] = None
    if request.multipart is not None:
        # The HTTP library has to generate the multipart boundary itself, so no
        # Content-Type is set here.
        part = request.multipart
        files = {part.name: (part.filename, part.content, part.content_type)}
    elif request.body is not UNSET:
        headers["Content-Type"] = "application/json"
        content = encode_json(request.body)

    return Prepared(
        method=request.method,
        url=url,
        headers=headers,
        content=content,
        files=files,
        timeout=request.timeout if request.timeout is not None else config.timeout,
        mode=request.mode,
    )


# ---------------------------------------------------------------------------
# Reading a response
# ---------------------------------------------------------------------------


@dataclass
class RawResponse:
    status: int
    retry_after: Optional[str]
    content_type: Optional[str]
    body: bytes


def refuse_redirect(status: int) -> None:
    """Refuse any redirect.

    Requests go out without following redirects, so a 3xx comes back as a
    response. A 307/308 would replay pairing secrets, uploaded bytes or a
    webhook body to whatever origin ``Location`` names, so no redirect is ever
    followed, to the same origin or another.
    """
    if 300 <= status < 400:
        raise PingRoomError(
            "Refusing to follow a redirect; credentials and request bodies are never forwarded.",
            code="redirect_refused",
            status=status,
        )


def invalid_success_response(status: int, expected: str, body: Any) -> PingRoomError:
    """A 2xx the PingRoom API never sends: a gateway's or captive portal's page,
    or a URL that is not PingRoom's. The body is kept short, for inspection only."""
    return PingRoomError(
        f"PingRoom returned an invalid response (HTTP {status}): expected {expected}.",
        code="invalid_response",
        body=body[:512] if isinstance(body, str) else body,
    )


def decode_text(body: bytes) -> str:
    return body.decode("utf-8-sig", errors="replace")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def parse_json(text: str) -> Any:
    """Parse strict JSON: ``NaN`` and ``Infinity`` are refused, as in ``JSON.parse``."""
    return json.loads(text, parse_constant=_reject_constant)


def interpret(raw: RawResponse, mode: str = "json") -> Any:
    """Turn a response into a result, or raise a ``PingRoomError``."""
    refuse_redirect(raw.status)
    ok = 200 <= raw.status < 300
    if mode == "raw":
        if not ok:
            _raise_for_error(raw)
        return {"content": raw.body, "content_type": raw.content_type}
    if raw.status == 204:
        return None
    text = decode_text(raw.body)
    parsed: Any = None
    if js_trim(text):
        try:
            parsed = parse_json(text)
        except ValueError:
            # A non-JSON error stays text for the error mapping below; a
            # non-JSON success is a proxy's page, not data.
            if ok:
                raise invalid_success_response(raw.status, "a JSON body", text) from None
            parsed = text
    if not ok:
        raise PingRoomError.from_response(raw.status, parsed, raw.retry_after)
    return parsed


def _raise_for_error(raw: RawResponse) -> None:
    """Route a failed raw download through the shared JSON error mapping."""
    if raw.status == 204:
        raise PingRoomError.from_response(raw.status, None, raw.retry_after)
    text = decode_text(raw.body)
    parsed: Any = None
    if js_trim(text):
        try:
            parsed = parse_json(text)
        except ValueError:
            parsed = text
    raise PingRoomError.from_response(raw.status, parsed, raw.retry_after)


# ---------------------------------------------------------------------------
# Small helpers shared by the operations
# ---------------------------------------------------------------------------


def error_description(exc: BaseException) -> str:
    """A transport exception as one line: its class and its message."""
    name = type(exc).__name__
    text = str(exc)
    return f"{name}: {text}" if text else name


def redact_url_in_message(message: str, raw_url: str) -> str:
    """Strip a URL out of an error message, keeping only its origin.

    An incoming-webhook URL carries its secret in the path, and HTTP libraries
    quote the requested URL in their errors, so a message copied from one must
    not reach a caller's logs as it is.
    """
    parts = urlsplit(raw_url.strip())
    origin = f"{parts.scheme}://{_host_display(parts.hostname or '', parts.port)}"
    withheld = f"{origin}/[redacted]"
    out = message
    for full in (raw_url, raw_url.strip(), parts.geturl()):
        if full:
            out = out.replace(full, withheld)
    query = f"?{parts.query}" if parts.query else ""
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    for tail in (f"{parts.path}{query}{fragment}", parts.path):
        if len(tail) > 1:
            out = out.replace(tail, "/[redacted]")
    return out
