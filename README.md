# PingRoom for Python

The official Python SDK for the [PingRoom](https://pingroom.io) agent API. Send a Ping to a room's members, ask a human a question and wait for the answer, drive a live card on their lock screen, and verify the webhooks PingRoom sends back.

It mirrors the TypeScript SDK, [`@pingroom/sdk`](https://github.com/pingroom/sdk), call for call: the same requests, the same local checks, the same wait-loop timing. There is a blocking client (`PingRoom`) and an asynchronous one (`AsyncPingRoom`) with the same methods.

## Install

```sh
pip install pingroom
```

Python 3.9 or later. The only dependency is [`httpx`](https://www.python-httpx.org/).

## Quick start

```python
from pingroom import PingRoom

client = PingRoom(token="agt_...")  # the credential your human approved

ping = client.broadcast("ab12cd", "Deploy shipped", data={"version": "1.4.0"})
print(ping["id"])

question = client.questions.ask(
    "ab12cd", "Ship 1.4.0 to production?", options=["ship", "hold"], idempotency_key="deploy-1.4.0"
)
answer = client.questions.wait_for_answer(question["id"])  # blocks until they tap
if answer["state"] == "answered" and answer["answer"]["value"] == "ship":
    ...
```

Results are plain `dict`s: the JSON the API sent, untouched, so a field the server adds later is still there. The types in `pingroom` (`PingResult`, `Question`, ...) are `TypedDict`s for your editor and type checker.

Room invite codes (`"ab12cd"`) identify rooms. Arguments are `snake_case` and optional ones are keyword-only; leaving one as `None` leaves it out of the request.

## Async

Every method exists on `AsyncPingRoom` as a coroutine, and `listen()` returns an async iterator.

```python
import asyncio
from pingroom import AsyncPingRoom


async def main() -> None:
    async with AsyncPingRoom(token="agt_...") as client:
        await client.broadcast("ab12cd", "Deploy shipped")


asyncio.run(main())
```

Both clients run the same operation code (`pingroom/_ops.py`); they differ only in the I/O. A test checks that their methods and signatures match.

## Sending a Ping

```python
client.broadcast(
    "ab12cd",
    "Prod is down",
    title="Alert",  # at most 40 characters
    is_urgent=True,  # breaks through Focus; asks nothing of the recipient
    tags=["incident", "prod"],  # up to 10, for filtering history later
    data={"service": "api"},  # at most 25 keys and 8 KB (as PHP encodes it)
    idempotency_key="incident-77",  # see below
)
```

A message is at most 120 characters in a private room and 160 in a public one. The SDK checks the 160 ceiling, because an invite code does not say which kind of room it is; the server applies 120 to a private room.

`is_urgent` and `requires_ack` are independent. `requires_ack=True` (with `ack_mode="any"` or `"all"` and an optional `ack_timeout_seconds` of 60 to 86400) holds the Ping open until someone taps Acknowledge.

**Idempotency.** A send that times out may still have been delivered. Pass `idempotency_key` and reuse the same key when you retry: the server replays the first result instead of sending again, so a retry with the same key is safe. The key is sent as the `Idempotency-Key` header, never in the body. It is 1 to 255 printable ASCII characters (spaces allowed); surrounding whitespace is trimmed as the server does, and anything else (an emoji, Cyrillic) is refused before sending with `invalid_request`, because it cannot travel as a header. Every send takes one: `broadcast`, `approvals.request`, `questions.ask`, `handoffs.request_ack`, `handoffs.ask` and `send_incoming_webhook`. An empty key means "no key" on the handoffs and the webhook, and is refused on the others.

**Link, location and buttons.** These build parts of `data`:

```python
from pingroom import link_ping, location_ping, ping_action

client.broadcast(
    "ab12cd",
    "Build 512 ready",
    data={
        "commit": "abc123",
        **link_ping("https://ci.example.com/b/512", button_label="Open build"),
        **location_ping(25.2048, 55.2708, label="Dubai Mall"),
    },
)

ping = client.broadcast(
    "ab12cd",
    "Disk 91% on db-2",
    data={
        "actions": [  # up to three buttons
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
secret = ping["action_signing_secret"]  # keep it: taps on "Clean up now" are signed with it
```

`link_ping`, `location_ping` and `ping_action` check their input the way the server does. `link_ping` and `location_ping` raise `ValueError` or `TypeError`; `ping_action` raises `PingRoomError` with code `invalid_data`. A `webhook` button needs a room owner on Pro, and buttons are refused in public and personal rooms, with `requires_ack`, and on every route except `broadcast` and the incoming webhook. See [Verifying webhooks](#verifying-webhooks) for the signed tap.

**Keep updating one Ping.** `replace_key="build:41"` makes a send replace your earlier Ping with that key (in the same room, while it is replaceable) and return it with `replaced: True`.

## Reading, replacing and taking back Pings

```python
page = client.notifications.list(tag="deploy", limit=10)  # history; each row has room["code"]
ping = client.notifications.get(ping_id)  # one Ping, with its action_state

client.notifications.replace("ab12cd", ping_id, "Build 41: green")  # title and data kept
client.notifications.replace("ab12cd", ping_id, "Build 41: green", title=None)  # None clears it
client.notifications.withdraw("ab12cd", ping_id)  # removes it from lock screens

result = client.notifications.wait_for_acknowledgement(ping_id, timeout=30)
# result["action_state"]["status"] is "open", "acked" or "expired"; "open" means this hold ran out, call again
```

`replace()` changes the notification on each phone in place, silently. Only an ordinary Ping you sent in a private room, under 24 hours old and replaced fewer than 20 times, can be replaced or withdrawn. `title` and `data` keep their value when left out (the default is `pingroom.UNSET`) and are cleared by `None`.

### Listening

```python
import threading

stop = threading.Event()

for ping in client.notifications.listen(cancel=stop):
    print(ping["message"])
    if ping["message"] == "stop":
        stop.set()
```

`listen()` reads the current head first, then long-polls forward, so only Pings that land after the call are yielded. It leaves out everything sent as the connected account (your own sends, your human's replies, other agents on the same account), so a "stop" has to come from someone else. A single poll is `notifications.wait(after=cursor, timeout=20)`.

Transient failures (network errors, timeouts, 429, 5xx) are retried with a backoff that starts at `backoff` seconds (default 1, minimum 0.25), doubles to 30 seconds and waits out a server `Retry-After`; `on_error` sees each one. Any other error, such as a 401 after the credential is revoked, ends the iterator by raising. Polls after an empty answer start at least 2.1 seconds apart, so one loop stays under the route's 30 requests a minute.

## Asking humans

```python
approval = client.approvals.request(
    "ab12cd",
    "Ship 1.4.0?",
    options=["Ship it", "Hold"],
    correlation_id="deploy-1.4.0",
    idempotency_key="deploy-1.4.0-approval",
)
decided = client.approvals.wait_for_decision(approval["id"])
if decided["status"] == "decided" and decided["decision"] == "Ship it":
    ...  # gate on the exact decision, never on `!= "Hold"`

poll = client.questions.ask(
    "ab12cd", "Lunch?", options=["pizza", "tacos"], resolution_policy="all", ttl=600
)
done = client.questions.wait_for_answer(poll["id"])  # done["results"] holds the tally

task = client.handoffs.request_ack(
    "Deploy 1.4.0 is queued. Ack to proceed.", idempotency_key="deploy-1.4.0"
)
result = client.handoffs.wait_for_result(task["id"])  # result["state"] is "acked", "expired", ...
choice = client.handoffs.ask("Ship or hold?", ["deploy", "hold"])
```

An approval's `status` is `pending`, `decided`, `expired` or `cancelled`. A handoff goes to exactly one human (the one who connected the agent) and is private. A negative answer such as `hold` is a successful `answered` result, not an error.

The three `wait_for_*` calls loop a bounded long-poll. A response that is not the id you asked about, in a known state, raises `invalid_response` instead of being returned as an outcome. They retry network errors, 429 and 5xx with the backoff above (honouring `Retry-After`), start polls at least 2.1 seconds apart, and are bounded without a cancel signal: once only failures have come back since the last valid response, the wait raises `unavailable` (the last failure is `__cause__`) 5 minutes after the last `expires_at` it read, or after 15 minutes of failures if it has not read one. `on_retry(error, attempt)` sees each retry, and raising from it ends the wait with that error.

`cancel` stops a wait:

* On `PingRoom` it is a `threading.Event`. It is checked before every request and during every pause, so a wait stops within one hold time (20 seconds by default; pass `timeout=` to `handoffs.wait_for_result` to shorten it). A request already in flight is not interrupted. On a single poll (`wait`, `wait_for_acknowledgement`, `attachments.content`) it only stops a request that has not gone out yet.
* On `AsyncPingRoom` it is an `asyncio.Event` (or just cancel the task), and it abandons the request in flight.

A cancelled wait raises `PingRoomCancelledError`; `listen()` just ends.

## Live status

A self-updating card on room members' lock screens, keyed by `correlation_id`. Every template has a builder in `pingroom.live_status`: `status`, `steps`, `progress` (with `segments`), `metrics`, `countdown`, `question`, `matchup`, `claim`, `timer`, `value` and `stats`, plus `alert`, `done` and `failed`.

```python
from pingroom import live_status

client.live.push(
    "ab12cd",
    live_status.progress("deploy-41", {"state": "running", "progress": 0.4, "message": "Building"}),
)
client.live.push("ab12cd", live_status.done("deploy-41", "Live"))

snapshot = client.live.get("ab12cd", "deploy-41")  # None only for a 404 stream_not_found
```

"Who's on it?" requests are streams one room member takes on: start one with `live_status.claim(...)`, and move it with `client.live.claim("ab12cd", notification_id, "claim")` (or `"release"`, `"done"`, `"cancel"`). A malformed or mismatched receipt raises `invalid_response`, and the SDK never repeats the transition POST on its own.

## Room metrics

Named values a script keeps in a room for a phone widget. Setting one sends no Ping.

```python
client.metrics.set("ab12cd", "mrr", 12400, label="MRR", format="currency", unit="USD")
client.metrics.list("ab12cd")  # {"metrics": [...], "limit": 8}
client.metrics.delete("ab12cd", "mrr")
```

## Attachments

```python
attachment = client.attachments.upload(
    open("report.pdf", "rb").read(), "report.pdf", content_type="application/pdf"
)
client.broadcast("ab12cd", "Report attached", attachment_ids=[attachment["id"]])

client.attachments.content(
    attachment["id"]
)  # {"content": b"...", "content_type": "application/pdf"}
client.attachments.manifest(zip_attachment_id)  # a .zip's listing, or None
client.attachments.delete(attachment["id"])  # only while it is not attached to a Ping
```

A file is 1 byte to 5 MiB (larger is refused locally with `attachment_too_large`) and its name ends in md, pdf, html, txt, jpg, jpeg, png or zip. Uploading needs a Pro account (`402 pro_required` otherwise). A Ping takes at most four.

## Incoming webhooks

A room's incoming-webhook URL carries its own secret, so no token is needed:

```python
from pingroom import send_incoming_webhook

send_incoming_webhook(
    "https://api.pingroom.io/api/webhooks/AB12/secret",
    {"title": "Build 512", "message": "green", "tags": ["ci"], "replace_key": "build:512"},
    idempotency_key="build-512",
)
```

The payload uses the endpoint's own field names: `title`, `message`, `action` (`None` bypasses the webhook's stored preset), `emoji`, `icon`, `color`, `sound`, `data` (including `data.actions`), `correlation_id`, `reply_to`, `tags`, `is_urgent`, `requires_ack`, `ack_mode`, `ack_timeout_seconds`, `live_status`, `metric` and `replace_key`. Any other key is ignored. Put `live_status` or `metric` at the top level, never inside `data`. `send_incoming_webhook_async` is the coroutine twin. Errors never echo the URL.

## Verifying webhooks

PingRoom signs each outgoing webhook, and each tap on a Ping's `webhook` button (with that Ping's `action_signing_secret`), with the same scheme. Verify over the exact raw bytes of the request body:

```python
from pingroom import verify_webhook_signature

ok = verify_webhook_signature(
    payload=request.get_data(),  # raw bytes, before any JSON parsing
    secret=signing_secret,
    timestamp=request.headers.get("X-PingRoom-Timestamp"),
    signature_v2=request.headers.get("X-PingRoom-Signature-V2"),
    delivery_id=request.headers.get("X-PingRoom-Delivery"),
    require_v2=True,  # refuses v1-only and stale requests
)
if not ok:
    abort(401)
```

* The comparison is constant-time. A mismatch returns `False`; it never raises.
* `payload` must be `bytes`, `bytearray` or `memoryview`. A `dict`, `None` or a decoded `str` (what a body parser that did not keep the raw bytes leaves behind) raises `TypeError`, so a receiver bug cannot look like a forged signature. Flask: `request.get_data()`. Django: `request.body`. Starlette and FastAPI: `await request.body()`.
* When `signature_v2` is present it must verify and v1 is never tried. Set `require_v2=True` so a delivery without it is refused: v1 does not sign `X-PingRoom-Delivery`, so a captured body replayed under a new delivery id still passes v1. Every current delivery carries v2.
* A timestamp more than `max_age_seconds` (default 300) from now, in either direction, is refused.
* Dedupe on `X-PingRoom-Delivery`: a delivery is retried on a 429, a 5xx or no answer.
* For a button tap, read `notification_id` from the raw body only to look up the secret you kept from the send, then verify before trusting anything in the body.

## Errors

Every failure raises `PingRoomError`, with:

| Attribute | |
| --- | --- |
| `status` | the HTTP status, or `0` for a client-side or network error |
| `code` | the API's machine code (`room_not_granted`, `cooldown_active`, `rate_limited`, ...), or a client code |
| `message` | the server's message |
| `retry_after` | seconds, from the body or a `Retry-After` header, when given |
| `body` | the parsed response body |

Subclasses: `PingRoomTimeoutError` (`timeout`), `PingRoomNetworkError` (`network_error`) and `PingRoomCancelledError` (`cancelled`). The client codes are `no_token`, `invalid_request` (input refused before sending), `invalid_data` (a bad `data` or button), `invalid_url`, `insecure_url`, `redirect_refused`, `invalid_response` (a 2xx that is not what the API sends, such as a proxy's page) and `unavailable` (a wait gave up).

Retry on the status, not on one code: route throttles answer `rate_limited`, but a public room's limit answers `public_room_rate_limited` and a quick action's cooldown `cooldown_active`. Every 429 should be retried after `retry_after`, with the same `idempotency_key`:

```python
import time
from pingroom import PingRoomError

try:
    client.broadcast("ab12cd", "hi", idempotency_key="hello-1")
except PingRoomError as error:
    if error.status == 429:
        time.sleep(error.retry_after or 1)
        client.broadcast("ab12cd", "hi", idempotency_key="hello-1")
    else:
        raise
```

The server's code vocabulary is exported as tuples, one per surface: `HANDOFF_ERROR_CODES`, `ROOM_SCOPED_ERROR_CODES`, `REPLACE_ERROR_CODES`, `WITHDRAW_ERROR_CODES`, `METRIC_ERROR_CODES`, `LIVE_CLAIM_ERROR_CODES`, `PING_ACTION_ERROR_CODES`, `COMMON_ERROR_CODES`, `REQUEST_LIMIT_ERROR_CODES`, plus `QUICK_ACTION_ERROR_CODES` and `AGENT_INBOX_ERROR_CODES` for surfaces this SDK does not wrap yet. Tests pin them to the TypeScript SDK's lists.

## Configuration

```python
PingRoom(
    token="...",  # optional for calls that need none
    base_url="https://api.pingroom.io",  # or env PINGROOM_API_URL / PINGROOM_BASE_URL
    timeout=30,  # seconds per request; long polls use their hold time + 10
    transport=None,  # an httpx transport, e.g. httpx.MockTransport in tests
    user_agent=None,  # default "pingroom-python/<version>"
    allow_insecure=False,  # allow plain http to a non-loopback host
)
```

* **HTTPS is required.** Plain `http` is refused except to `localhost`, `127.0.0.1`, `::1` and `*.localhost`, unless `allow_insecure=True`.
* **Redirects are never followed**, to the same origin or another. A 3xx raises `redirect_refused`, so a token, an upload or a webhook body is never forwarded.
* **The token stays out of sight.** It is held in an object whose `repr` is `[redacted]`, so `repr(client)`, `vars(client)` and a traceback never show it, no exception message contains it, and it cannot be pickled by accident. Read it back with `client.get_token()`; replace it with `client.set_token(...)`.
* **Timeouts.** `timeout` bounds each connect, read and write, and a body still arriving after that many seconds in total is abandoned.
* **No telemetry, no logging.** The SDK sends nothing but your requests and never logs a body or a token.

Use a client as a context manager (`with PingRoom(...) as client:`), or call `close()` (`await aclose()`).

## Testing your code

Pass an `httpx.MockTransport` as `transport`:

```python
import httpx
from pingroom import PingRoom


def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"id": "n1", "message": "hi", "created_at": "now"})


client = PingRoom(token="t", transport=httpx.MockTransport(handler))
```

A custom transport must raise `httpx` exceptions for failures (`httpx.ConnectError`, `httpx.ReadTimeout`, ...), as real ones do; they become `PingRoomNetworkError` and `PingRoomTimeoutError`.

## Not yet

Version 0.1.0 does not include:

* agent registration, pairing and OAuth helpers (`auth`),
* managing rooms (`rooms`), incoming webhooks (`webhooks`) and quick actions (`actions`),
* the agent directory, the agent profile and the Agent Inbox (`inbox`),
* redeeming gift codes,
* the MCP JSON-RPC client.

Use the TypeScript SDK, the CLI or the MCP server for those.

## Differences from the TypeScript SDK

* Names are `snake_case`, optional arguments are keyword-only, results are `dict`s, and SDK-only options are `idempotency_key`, `cancel`, `on_retry`, `timeout` (seconds) and `transport` (an `httpx` transport instead of `fetch`).
* `None` means "leave it out". Where the API distinguishes `null` from absent, the argument defaults to `pingroom.UNSET` (`notifications.replace`), or the field is a key in a `payload` mapping (`send_incoming_webhook`, which also takes `replace_key` under its wire name).
* `verify_webhook_signature` takes keyword arguments and raw bytes only (the TypeScript one also accepts a string).
* A failing `httpx` call is not kept as `__cause__` of the `PingRoomError`, because the exception holds the request and its headers; its class and message are in the error message.

## Compatibility

Covers the PingRoom agent API as of 2026-11-11: Pings with structured `data` (25 keys, 8 KB), tags, buttons (`data.actions`) and signed button deliveries, `replace_key`, replacing and withdrawing a Ping, urgency and confirmation modes (`ack_mode`), link and location Pings, attachments, Questions and polls, approvals, handoffs, room metrics, live status with every template (`status`, `steps`, `progress` with segments, `metrics`, `countdown`, `question`, `matchup`, `claim`, `timer`, `value`, `stats`) and "Who's on it?" claims, incoming webhooks (including `live_status` and `metric` bodies), and V1 and V2 webhook signatures. It matches `@pingroom/sdk` 0.9.0 plus that package's unreleased additions of the same date.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy                  # strict
pytest                # no network: every test uses httpx.MockTransport
```

`tests/contracts/` holds copies of PingRoom's cross-repo contract fixtures (see its README for how to refresh them) and `tests/golden/` the requests the TypeScript SDK sends for the same calls; the tests compare this SDK against both.

## License

MIT
