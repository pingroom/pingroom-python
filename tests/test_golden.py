"""Byte-for-byte parity with the TypeScript SDK.

``tests/golden/scenarios.json`` lists one call per public method (and several
awkward inputs: quotes, control characters, emoji, ``/`` in ids, empty and
trimmed idempotency keys). ``tests/golden/ts-requests.json`` is what the
TypeScript SDK actually sent for each (recorded by ``tests/golden/generate.mjs``).
Every scenario is replayed through this SDK, blocking and asynchronous, and the
method, path with query, the headers that matter and the raw JSON body must be
identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import httpx
import pytest

from conftest import Harness
from pingroom import send_incoming_webhook, send_incoming_webhook_async

GOLDEN = Path(__file__).parent / "golden"
SCENARIOS: List[Dict[str, Any]] = json.loads((GOLDEN / "scenarios.json").read_text("utf-8"))
RECORDED = json.loads((GOLDEN / "ts-requests.json").read_text("utf-8"))
TS_BY_ID = {entry["id"]: entry for entry in RECORDED["scenarios"]}
KEPT_HEADERS = ("accept", "authorization", "content-type", "idempotency-key")


def _pop(args: Dict[str, Any], *names: str) -> List[Any]:
    return [args.pop(name) for name in names]


def _broadcast(c: Any, a: Dict[str, Any]) -> Any:
    invite_code, message = _pop(a, "invite_code", "message")
    return c.broadcast(invite_code, message, **a)


def _replace(c: Any, a: Dict[str, Any]) -> Any:
    options = {name: a[name] for name in ("title", "data") if name in a}
    return c.notifications.replace(a["invite_code"], a["notification_id"], a["message"], **options)


def _approval_request(c: Any, a: Dict[str, Any]) -> Any:
    invite_code, question = _pop(a, "invite_code", "question")
    return c.approvals.request(invite_code, question, **a)


def _question_ask(c: Any, a: Dict[str, Any]) -> Any:
    invite_code, prompt = _pop(a, "invite_code", "prompt")
    return c.questions.ask(invite_code, prompt, **a)


def _request_ack(c: Any, a: Dict[str, Any]) -> Any:
    (prompt,) = _pop(a, "prompt")
    return c.handoffs.request_ack(prompt, **a)


def _handoff_ask(c: Any, a: Dict[str, Any]) -> Any:
    prompt, options = _pop(a, "prompt", "options")
    return c.handoffs.ask(prompt, options, **a)


def _metric_set(c: Any, a: Dict[str, Any]) -> Any:
    invite_code, key, value = _pop(a, "invite_code", "key", "value")
    return c.metrics.set(invite_code, key, value, **a)


CALLS: Dict[str, Callable[[Any, Dict[str, Any]], Any]] = {
    "broadcast": _broadcast,
    "notifications.list": lambda c, a: c.notifications.list(**a),
    "notifications.get": lambda c, a: c.notifications.get(a["notification_id"]),
    "notifications.wait": lambda c, a: c.notifications.wait(**a),
    "notifications.wait_for_acknowledgement": lambda c, a: c.notifications.wait_for_acknowledgement(
        a["notification_id"], timeout=a["timeout"]
    ),
    "notifications.replace": _replace,
    "notifications.withdraw": lambda c, a: c.notifications.withdraw(
        a["invite_code"], a["notification_id"]
    ),
    "approvals.request": _approval_request,
    "approvals.get": lambda c, a: c.approvals.get(a["approval_id"]),
    "approvals.wait": lambda c, a: c.approvals.wait(a["approval_id"], timeout=a.get("timeout")),
    "approvals.wait_for_decision": lambda c, a: c.approvals.wait_for_decision(a["approval_id"]),
    "questions.ask": _question_ask,
    "questions.get": lambda c, a: c.questions.get(a["question_id"]),
    "questions.wait": lambda c, a: c.questions.wait(a["question_id"], timeout=a.get("timeout")),
    "questions.wait_for_answer": lambda c, a: c.questions.wait_for_answer(a["question_id"]),
    "questions.list": lambda c, a: c.questions.list(**a),
    "questions.cancel": lambda c, a: c.questions.cancel(a["question_id"]),
    "handoffs.request_ack": _request_ack,
    "handoffs.ask": _handoff_ask,
    "handoffs.get": lambda c, a: c.handoffs.get(a["handoff_id"]),
    "handoffs.wait_for_result": lambda c, a: c.handoffs.wait_for_result(
        a["handoff_id"], timeout=a.get("timeout")
    ),
    "handoffs.list": lambda c, a: c.handoffs.list(**a),
    "metrics.list": lambda c, a: c.metrics.list(a["invite_code"]),
    "metrics.set": _metric_set,
    "metrics.delete": lambda c, a: c.metrics.delete(a["invite_code"], a["key"]),
    "attachments.manifest": lambda c, a: c.attachments.manifest(a["attachment_id"]),
    "attachments.content": lambda c, a: c.attachments.content(a["attachment_id"]),
    "attachments.delete": lambda c, a: c.attachments.delete(a["attachment_id"]),
    "live.push": lambda c, a: c.live.push(a["invite_code"], a["ping"]),
    "live.get": lambda c, a: c.live.get(a["invite_code"], a["correlation_id"]),
    "live.claim": lambda c, a: c.live.claim(a["invite_code"], a["notification_id"], a["action"]),
}


def test_every_recorded_scenario_has_a_scenario_and_the_other_way_round() -> None:
    assert [s["id"] for s in SCENARIOS] == [e["id"] for e in RECORDED["scenarios"]]
    assert RECORDED["typescript_sdk_version"]
    assert {s["call"] for s in SCENARIOS} <= set(CALLS) | {"webhook"}


def test_every_public_method_in_scope_has_a_golden_scenario() -> None:
    covered = {s["call"] for s in SCENARIOS}
    expected = set(CALLS) | {"webhook"}
    assert covered == expected


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_the_request_is_what_the_typescript_sdk_sends(
    harness: Harness, scenario: Dict[str, Any]
) -> None:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = scenario["response"]
        content = b"" if response["body"] is None else json.dumps(response["body"]).encode()
        headers = (
            {"Content-Type": "application/pdf"} if scenario["call"] == "attachments.content" else {}
        )
        return httpx.Response(response["status"], content=content, headers=headers)

    args = json.loads(json.dumps(scenario["args"]))  # a private copy: the call pops from it
    if scenario["call"] == "webhook":
        options = {"idempotency_key": args["idempotency_key"]} if "idempotency_key" in args else {}
        transport = httpx.MockTransport(handler)
        if harness.is_async:
            import asyncio

            asyncio.run(
                send_incoming_webhook_async(
                    args["url"], args["payload"], transport=transport, **options
                )
            )
        else:
            send_incoming_webhook(args["url"], args["payload"], transport=transport, **options)
    else:
        client = harness.client(handler, token="tok_test")
        harness.run(client, lambda c: CALLS[scenario["call"]](c, args))

    recorded = TS_BY_ID[scenario["id"]]
    assert recorded["error"] is None
    assert len(seen) == len(recorded["requests"])
    for request, expected in zip(seen, recorded["requests"]):
        assert request.method == expected["method"]
        assert request.url.raw_path.decode() == expected["path"]
        headers = {name: request.headers[name] for name in KEPT_HEADERS if name in request.headers}
        if scenario["call"] == "webhook":
            headers.pop("authorization", None)  # a webhook URL carries its own secret
            expected_headers = {
                k: v for k, v in expected["headers"].items() if k != "authorization"
            }
        else:
            expected_headers = expected["headers"]
        assert headers == expected_headers
        raw = request.content.decode("utf-8") if request.content else None
        assert raw == expected["body"]
