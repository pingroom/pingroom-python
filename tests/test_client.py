"""Client construction, headers, error mapping, redirects, HTTPS, token redaction, timeouts."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
import threading
import time
import traceback
from typing import Any, Callable, List

import httpx
import pytest

import pingroom
from conftest import Harness, Recorder, reply, static
from pingroom import (
    AsyncPingRoom,
    PingRoom,
    PingRoomError,
    PingRoomNetworkError,
    PingRoomTimeoutError,
)

TOKEN = "agt_live_fixture_credential_5f2c9e"


def serve_text(text: str, status: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: reply(text=text, status=status)


def serve_bytes(status: int, content: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, content=content)


def test_version_is_written_once_and_used_for_the_user_agent(harness: Harness) -> None:
    assert pingroom.__version__ == "0.1.0"
    rec = Recorder(fallback=static({"data": []}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.list())
    assert rec.last.headers["user-agent"] == "pingroom-python/0.1.0"


def test_a_call_sends_bearer_auth_and_json_accept(harness: Harness) -> None:
    rec = Recorder(fallback=static({"id": "n1"}))
    client = harness.client(rec, token=TOKEN)
    harness.run(client, lambda c: c.broadcast("ab12", "hi"))
    request = rec.last
    assert request.method == "POST"
    assert str(request.url) == "https://api.pingroom.io/api/agent/rooms/ab12/notifications"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"


def test_user_agent_can_be_overridden(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec, user_agent="my-bot/2")
    harness.run(client, lambda c: c.questions.get("q1"))
    assert rec.last.headers["user-agent"] == "my-bot/2"


def test_set_token_updates_the_bearer_used_on_later_calls(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec, token=None)
    client.set_token("tok2")
    harness.run(client, lambda c: c.questions.get("q1"))
    assert rec.last.headers["authorization"] == "Bearer tok2"
    assert client.get_token() == "tok2"
    client.set_token(None)
    assert client.get_token() is None


def test_a_call_that_needs_auth_without_a_token_fails_before_any_request(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec, token=None)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.code == "no_token"
    assert caught.value.status == 0
    assert rec.requests == []


# --- base URL --------------------------------------------------------------


def test_base_url_defaults_and_reads_the_environment(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PINGROOM_API_URL", raising=False)
    monkeypatch.delenv("PINGROOM_BASE_URL", raising=False)
    assert harness.client().base_url == "https://api.pingroom.io"
    monkeypatch.setenv("PINGROOM_BASE_URL", "https://b.example.com/")
    assert harness.client().base_url == "https://b.example.com"
    monkeypatch.setenv("PINGROOM_API_URL", "https://a.example.com/api")
    assert harness.client().base_url == "https://a.example.com/api"
    # An explicit argument wins over both variables.
    assert harness.client(base_url="https://explicit.example.com").base_url == (
        "https://explicit.example.com"
    )


def test_base_url_keeps_a_path_prefix_and_drops_query_and_trailing_slash(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec, base_url="https://gw.example.com/pingroom/?x=1#frag")
    harness.run(client, lambda c: c.questions.get("q1"))
    assert str(rec.last.url) == "https://gw.example.com/pingroom/api/agent/questions/q1"


def test_an_insecure_base_url_is_refused_at_construction(harness: Harness) -> None:
    with pytest.raises(PingRoomError) as caught:
        harness.client(base_url="http://api.evil.com")
    assert caught.value.code == "insecure_url"


def test_http_is_allowed_for_loopback_hosts_and_when_asked(harness: Harness) -> None:
    for url in (
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://api.localhost",
    ):
        harness.client(base_url=url)
    harness.client(base_url="http://internal.corp", allow_insecure=True)
    for url in ("http://127.0.0.2", "http://localhost.evil.com", "ftp://localhost"):
        with pytest.raises(PingRoomError) as caught:
            harness.client(base_url=url)
        assert caught.value.code == "insecure_url", url


def test_a_malformed_base_url_never_echoes_its_value(harness: Harness) -> None:
    with pytest.raises(PingRoomError) as caught:
        harness.client(base_url="not-a-url/secret-SYNTHETIC")
    assert caught.value.code == "invalid_url"
    assert "secret-SYNTHETIC" not in caught.value.message


def test_a_non_positive_timeout_is_refused(harness: Harness) -> None:
    for timeout in (0, -1, float("nan"), True):
        with pytest.raises(PingRoomError) as caught:
            harness.client(timeout=timeout)
        assert caught.value.code == "invalid_request"


# --- the token stays out of every rendering --------------------------------


def test_the_token_is_absent_from_repr_str_vars_and_pickle(harness: Harness) -> None:
    client = harness.client(token=TOKEN)
    renderings = [
        repr(client),
        str(client),
        f"{client}",
        repr(vars(client)),
        repr(vars(client._config)),
        repr(client._config),
        repr(client.notifications.__dict__),
        repr(client.live.__dict__),
    ]
    for rendering in renderings:
        assert TOKEN not in rendering, rendering[:200]
    assert "[redacted]" in repr(client)
    assert repr(client).startswith(("PingRoom(", "AsyncPingRoom("))
    assert client.get_token() == TOKEN  # still readable through the explicit accessor
    with pytest.raises(TypeError):
        pickle.dumps(client._config.token)
    with pytest.raises(TypeError):
        copy.copy(client._config.token)


def test_a_client_without_a_token_says_so() -> None:
    assert "token=None" in repr(PingRoom())


def test_the_token_never_appears_in_an_error(harness: Harness) -> None:
    client = harness.client(
        static({"code": "pings_closed", "message": "closed"}, status=403), token=TOKEN
    )
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("AB12", "hi"))
    error = caught.value
    assert error.code == "pings_closed"
    assert error.status == 403
    assert TOKEN not in repr(error)
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error.__dict__)
    assert TOKEN not in "".join(traceback.format_exception(type(error), error, error.__traceback__))


def test_a_network_error_carries_no_credential_and_no_cause(harness: Harness) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = harness.client(boom, token=TOKEN)
    with pytest.raises(PingRoomNetworkError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    error = caught.value
    assert error.code == "network_error"
    assert error.status == 0
    assert "ConnectError: connection refused" in error.message
    assert error.__cause__ is None  # the httpx exception holds the request, and its headers
    assert TOKEN not in repr(error.__dict__)


# --- error mapping ---------------------------------------------------------


def test_error_codes_come_from_code_then_error_and_the_message_falls_back(
    harness: Harness,
) -> None:
    cases = [
        ({"code": "room_not_granted", "message": "nope"}, "room_not_granted", "nope"),
        ({"error": "cooldown_active", "message": "wait"}, "cooldown_active", "wait"),
        ({"error": "cooldown_active"}, "cooldown_active", "cooldown_active"),
        ({"message": "The route could not be found."}, None, "The route could not be found."),
        ({}, None, "HTTP 418"),
    ]
    for body, code, message in cases:
        client = harness.client(static(body, status=418))
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c: c.questions.get("q1"))
        assert (caught.value.code, caught.value.message, caught.value.status) == (
            code,
            message,
            418,
        )
        assert caught.value.body == body


def test_retry_after_comes_from_the_body_then_the_header(harness: Harness) -> None:
    client = harness.client(
        static({"code": "rate_limited", "retry_after": 7}, 429, {"Retry-After": "3"})
    )
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.retry_after == 7
    client = harness.client(static({"code": "rate_limited"}, 429, {"Retry-After": " 3 "}))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.retry_after == 3
    # An HTTP-date Retry-After is not a delay in seconds and is ignored.
    client = harness.client(static({}, 429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.retry_after is None


def test_a_non_json_error_body_still_maps_with_its_status_and_text(harness: Harness) -> None:
    client = harness.client(lambda request: reply(text="<html>Bad gateway</html>", status=502))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.status == 502
    assert caught.value.code is None
    assert caught.value.body == "<html>Bad gateway</html>"


def test_errors_survive_pickling() -> None:
    error = PingRoomError("m", status=409, code="claim_conflict", retry_after=2, body={"a": 1})
    again = pickle.loads(pickle.dumps(error))
    assert (again.message, again.status, again.code, again.retry_after, again.body) == (
        "m",
        409,
        "claim_conflict",
        2,
        {"a": 1},
    )
    timeout = pickle.loads(pickle.dumps(PingRoomTimeoutError()))
    assert isinstance(timeout, PingRoomTimeoutError)
    assert timeout.code == "timeout"


# --- success bodies --------------------------------------------------------


def test_a_2xx_whose_non_empty_body_is_not_json_is_invalid_response(harness: Harness) -> None:
    for text in ("<html>Please sign in to the hotspot</html>", "OK", '{"rooms": [', "undefined"):
        for status in (200, 201, 202):
            client = harness.client(serve_text(text, status))
            with pytest.raises(PingRoomError) as caught:
                harness.run(client, lambda c: c.questions.get("q1"))
            assert caught.value.code == "invalid_response"
            assert caught.value.status == 0
    client = harness.client(lambda request: reply(text="<html>" + "x" * 5000 + "</html>"))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert len(caught.value.body) <= 512


def test_nan_and_infinity_are_not_json(harness: Harness) -> None:
    client = harness.client(lambda request: reply(text='{"a": NaN}'))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.code == "invalid_response"


def test_an_empty_204_whitespace_or_null_success_is_still_a_success(harness: Harness) -> None:
    for status, text, expected in [
        (204, None, None),
        (200, "", None),
        (202, "", None),
        (200, " \n", None),
        (200, "null", None),
        (200, '{"id": "q1"}', {"id": "q1"}),
    ]:
        client = harness.client(serve_bytes(status, (text or "").encode()))
        assert harness.run(client, lambda c: c.questions.get("q1")) == expected


def test_unknown_server_fields_pass_through_untouched(harness: Harness) -> None:
    body = {"id": "q1", "state": "pending", "future_field": {"nested": [1, 2, 3]}}
    client = harness.client(static(body))
    assert harness.run(client, lambda c: c.questions.get("q1")) == body


# --- redirects -------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_no_redirect_is_followed_with_credentials(harness: Harness, status: int) -> None:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.pingroom.io":
            return httpx.Response(status, headers={"Location": "https://evil.example.com/collect"})
        return httpx.Response(200, json={"success": True})

    client = harness.client(handler, token=TOKEN)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.broadcast("ab12", "private"))
    assert caught.value.code == "redirect_refused"
    assert caught.value.status == status
    assert [r.url.host for r in seen] == ["api.pingroom.io"]


def test_a_redirect_to_the_same_origin_is_refused_too(harness: Harness) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "/elsewhere"})

    client = harness.client(handler)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload(b"fixture-private-attachment", "a.txt"))
    assert caught.value.code == "redirect_refused"
    assert calls == 1


# --- paths -----------------------------------------------------------------


def test_a_path_parameter_of_empty_dot_or_dotdot_is_refused(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    attempts = [
        lambda c: c.live.get("AB12", ".."),
        lambda c: c.approvals.get(".."),
        lambda c: c.approvals.wait_for_decision("."),
        lambda c: c.questions.cancel("."),
        lambda c: c.questions.get(".."),
        lambda c: c.metrics.list(""),
        lambda c: c.notifications.get(".."),
    ]
    for attempt in attempts:
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, attempt)
        assert caught.value.code == "invalid_request"
    assert rec.requests == []
    # Dots inside a segment are ordinary characters.
    harness.run(client, lambda c: c.approvals.get("..a"))
    assert rec.last.url.path == "/api/agent/approvals/..a"


def test_path_segments_are_percent_encoded_like_encodeURIComponent(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.notifications.get("n/1 ?"))
    assert rec.last.url.raw_path == b"/api/agent/notifications/n%2F1%20%3F"
    harness.run(client, lambda c: c.notifications.get("a'()*!~-_.é"))
    assert rec.last.url.raw_path == b"/api/agent/notifications/a'()*!~-_.%C3%A9"


# --- timeouts --------------------------------------------------------------


def test_a_request_timeout_is_a_timeout_error(harness: Harness) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = harness.client(slow)
    with pytest.raises(PingRoomTimeoutError) as caught:
        harness.run(client, lambda c: c.questions.get("q1"))
    assert caught.value.code == "timeout"
    assert caught.value.status == 0
    assert isinstance(caught.value, PingRoomError)


def test_the_timeout_option_reaches_httpx_and_long_polls_extend_it(harness: Harness) -> None:
    seen: List[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"notifications": [], "cursor": "c"})

    client = harness.client(handler, timeout=12.5)
    harness.run(client, lambda c: c.questions.list())
    harness.run(client, lambda c: c.notifications.wait(after="c0", timeout=5))
    harness.run(client, lambda c: c.notifications.wait(after="c0"))
    assert seen[0]["read"] == 12.5
    assert seen[1]["read"] == 15  # the 5 s hold plus the 10 s buffer
    assert seen[2]["read"] == 30  # the 20 s default hold plus 10 s


def test_a_server_that_trickles_a_body_past_the_deadline_times_out(harness: Harness) -> None:
    class Trickle(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __iter__(self) -> Any:
            for _ in range(5):
                time.sleep(0.05)
                yield b" "

        async def __aiter__(self) -> Any:
            for _ in range(5):
                await asyncio.sleep(0.05)
                yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Trickle())

    client = harness.client(handler, timeout=0.1)
    started = time.monotonic()
    with pytest.raises(PingRoomTimeoutError):
        harness.run(client, lambda c: c.questions.get("q1"))
    assert time.monotonic() - started < 0.24


def test_the_async_client_can_abandon_a_call_in_flight() -> None:
    started = threading.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    async def go() -> float:
        client = AsyncPingRoom("t", transport=httpx.MockTransport(hang))
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        began = time.monotonic()
        with pytest.raises(pingroom.PingRoomCancelledError):
            await client.notifications.wait(after="c0", timeout=0, cancel=cancel)
        await client.aclose()
        return time.monotonic() - began

    assert asyncio.run(go()) < 1.0


def test_json_body_matches_json_stringify_for_text(harness: Harness) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    harness.run(client, lambda c: c.broadcast("ab12", 'quote " back \\ nl \n é ☃ / end'))
    raw = rec.last.content.decode("utf-8")
    # Compact separators, non-ASCII sent as UTF-8, '/' not escaped: JSON.stringify's output.
    assert raw == '{"message":"quote \\" back \\\\ nl \\n é ☃ / end"}'
    assert json.loads(raw)["message"].endswith("/ end")


def test_context_managers_close_the_client() -> None:
    with PingRoom("t", transport=httpx.MockTransport(static({}))) as client:
        assert client.base_url == "https://api.pingroom.io"

    async def go() -> None:
        async with AsyncPingRoom("t", transport=httpx.MockTransport(static({}))) as aclient:
            assert aclient.base_url == "https://api.pingroom.io"

    asyncio.run(go())
