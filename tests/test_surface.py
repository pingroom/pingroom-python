"""The public surface: the blocking and asynchronous clients cannot drift, the error
vocabulary is the TypeScript SDK's, and the package is built the way it claims."""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import pingroom
from pingroom import async_client, client, errors

ROOT = Path(__file__).resolve().parent.parent

RESOURCES = [
    "Notifications",
    "Approvals",
    "Questions",
    "Handoffs",
    "Metrics",
    "Attachments",
    "Live",
]


def public_methods(cls: type) -> Dict[str, Any]:
    return {
        name: member
        for name, member in vars(cls).items()
        if not name.startswith("_") and (inspect.isfunction(member) or isinstance(member, property))
    }


def shape(function: Any) -> List[Tuple[str, Any, Any]]:
    """Parameter names, kinds and defaults: everything but the annotations."""
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(function).parameters.values()
        if p.name != "self"
    ]


@pytest.mark.parametrize("name", RESOURCES)
def test_each_resource_has_the_same_methods_with_the_same_parameters_in_both_clients(
    name: str,
) -> None:
    sync_cls = getattr(client, name)
    async_cls = getattr(async_client, f"Async{name}")
    sync_methods, async_methods = public_methods(sync_cls), public_methods(async_cls)
    assert sorted(sync_methods) == sorted(async_methods)
    for method, function in sync_methods.items():
        twin = async_methods[method]
        assert shape(function) == shape(twin), f"{name}.{method}: signatures drifted"
        if method == "listen":
            # Only the cancellation paragraph differs: an asyncio.Event also aborts the poll in flight.
            first = (inspect.getdoc(function) or "").splitlines()[0]
            assert first == (inspect.getdoc(twin) or "").splitlines()[0].replace(
                "an async iterator", "an iterator"
            )
        else:
            assert inspect.getdoc(function) == inspect.getdoc(twin), (
                f"{name}.{method}: docs drifted"
            )
        # Every blocking method is a coroutine function in the async client, except the
        # one that returns an async iterator.
        if method == "listen":
            assert not inspect.iscoroutinefunction(twin)
        else:
            assert inspect.iscoroutinefunction(twin), f"{name}.{method} should be async"
            assert not inspect.iscoroutinefunction(function)


def test_the_two_root_clients_have_the_same_methods_and_parameters() -> None:
    sync_methods = public_methods(pingroom.PingRoom)
    async_methods = public_methods(pingroom.AsyncPingRoom)
    renamed = {"close": "aclose"}
    assert sorted(renamed.get(n, n) for n in sync_methods) == sorted(async_methods)
    for name, function in sync_methods.items():
        if isinstance(function, property):
            continue
        twin = async_methods[renamed.get(name, name)]
        assert shape(function) == shape(twin), f"PingRoom.{name}: signatures drifted"
        assert inspect.getdoc(function) == inspect.getdoc(twin) or name == "close"


def test_the_root_constructors_take_the_same_arguments_except_the_transport_type() -> None:
    sync_params = shape(pingroom.PingRoom.__init__)
    async_params = shape(pingroom.AsyncPingRoom.__init__)
    assert sync_params == async_params
    assert [p[0] for p in sync_params] == [
        "token",
        "base_url",
        "timeout",
        "transport",
        "user_agent",
        "allow_insecure",
    ]
    assert all(kind is inspect.Parameter.KEYWORD_ONLY for _, kind, _ in sync_params[1:])


def test_the_resource_namespaces_are_the_documented_ones() -> None:
    expected = {
        "notifications",
        "approvals",
        "questions",
        "handoffs",
        "metrics",
        "attachments",
        "live",
    }
    for cls in (pingroom.PingRoom, pingroom.AsyncPingRoom):
        assert expected == set(cls.__annotations__)


def test_the_facades_hold_no_logic_of_their_own() -> None:
    """Every public method is a thin call into ``_ops`` (one operation per method)."""
    for module in (client, async_client):
        source = inspect.getsource(module)
        # No request, URL or validation code lives in a client module.
        for forbidden in ("Request(", "build_url", "assert_", "json.dumps", "httpx.Request"):
            assert forbidden not in source, f"{module.__name__} contains {forbidden}"


# --- error vocabulary ---------------------------------------------------------


# The TypeScript SDK's exported lists, copied from sdk/src/errors.ts (0.9.0).
TS_CODES: Dict[str, List[str]] = {
    "ROOM_SCOPED_ERROR_CODES": ["room_not_granted"],
    "HANDOFF_ERROR_CODES": [
        "feature_temporarily_unavailable",
        "handoff_room_unsupported",
        "invalid_target",
        "no_room_configured",
        "target_not_permitted",
        "recipient_not_ready",
        "idempotency_conflict",
        "invalid_idempotency_key",
        "capability_check_unavailable",
        "insufficient_scope",
        "free_limit_reached",
    ],
    "AGENT_INBOX_ERROR_CODES": [
        "activation_evidence_unavailable",
        "no_room_configured",
        "activation_room_muted",
        "activation_delivery_unavailable",
        "feature_temporarily_unavailable",
    ],
    "QUICK_ACTION_ERROR_CODES": [
        "quick_action_input_required",
        "quick_action_input_type",
        "action_not_configured",
        "pro_required",
        "quick_action_layout_changed",
        "quick_action_page_in_use",
        "location_trigger_owner_only",
        "location_trigger_inactive",
    ],
    "LIVE_CLAIM_ERROR_CODES": [
        "claim_conflict",
        "claim_own_request",
        "claim_private_rooms_only",
        "claim_not_found",
        "claim_request_invalid",
        "claim_must_start_running",
        "claim_deadline_out_of_range",
    ],
    "REPLACE_ERROR_CODES": [
        "notification_not_found",
        "not_ping_owner",
        "unsupported_room",
        "unsupported_question",
        "unsupported_handoff",
        "unsupported_live_status",
        "unsupported_claim",
        "unsupported_ack",
        "unsupported_checklist",
        "unsupported_source",
        "unsupported_actions",
        "replace_window_closed",
        "replace_limit_reached",
        "ping_withdrawn",
    ],
    "WITHDRAW_ERROR_CODES": [
        "notification_not_found",
        "not_ping_owner",
        "unsupported_room",
        "unsupported_question",
        "unsupported_handoff",
        "unsupported_live_status",
        "unsupported_claim",
        "unsupported_ack",
        "unsupported_checklist",
        "unsupported_source",
        "withdraw_window_closed",
    ],
    "METRIC_ERROR_CODES": ["metrics_limit_reached", "metric_not_found", "metric_exists"],
    "COMMON_ERROR_CODES": ["rate_limited", "feature_disabled"],
    "REQUEST_LIMIT_ERROR_CODES": ["payload_too_large", "attachment_too_large"],
    "PING_ACTION_ERROR_CODES": [
        "ping_buttons_agents_only",
        "ping_buttons_unsupported_room",
        "ping_buttons_with_ack",
        "ping_buttons_require_pro",
    ],
}


@pytest.mark.parametrize("name,codes", sorted(TS_CODES.items()))
def test_the_error_code_vocabulary_is_the_typescript_sdks(name: str, codes: List[str]) -> None:
    assert list(getattr(errors, name)) == codes
    assert getattr(pingroom, name) == getattr(errors, name)
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code)


def test_a_common_code_belongs_to_no_per_surface_list() -> None:
    for common in errors.COMMON_ERROR_CODES:
        for name in TS_CODES:
            if name != "COMMON_ERROR_CODES":
                assert common not in getattr(errors, name), name


def test_every_exported_name_exists_and_there_are_no_duplicates() -> None:
    assert len(pingroom.__all__) == len(set(pingroom.__all__))
    for name in pingroom.__all__:
        assert hasattr(pingroom, name), name


def test_the_error_hierarchy() -> None:
    assert issubclass(pingroom.PingRoomTimeoutError, pingroom.PingRoomError)
    assert issubclass(pingroom.PingRoomNetworkError, pingroom.PingRoomError)
    assert issubclass(pingroom.PingRoomCancelledError, pingroom.PingRoomError)
    assert issubclass(pingroom.PingRoomError, Exception)


# --- packaging ------------------------------------------------------------------


def test_the_version_is_written_in_one_place() -> None:
    version_file = (ROOT / "src" / "pingroom" / "_version.py").read_text()
    assert re.search(r'^__version__ = "0\.1\.0"$', version_file, re.M)
    assert pingroom.__version__ == "0.1.0"
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert not re.search(r"^version\s*=", pyproject, re.M)
    # No other module hard-codes it.
    for path in (ROOT / "src" / "pingroom").glob("*.py"):
        if path.name != "_version.py":
            assert "0.1.0" not in path.read_text(), path.name


def test_the_package_is_typed_and_ships_py_typed() -> None:
    assert (ROOT / "src" / "pingroom" / "py.typed").exists()
    assert "Typing :: Typed" in (ROOT / "pyproject.toml").read_text()


def test_the_only_runtime_dependency_is_httpx() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    match = re.search(r"^dependencies = \[(.*?)\]", pyproject, re.M | re.S)
    assert match is not None
    assert re.findall(r'"([A-Za-z0-9_.-]+)', match.group(1)) == ["httpx"]


def test_the_license_holder_text_matches_the_typescript_sdks() -> None:
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License\n\nCopyright (c) 2026 PingRoom\n")


def test_no_module_logs_or_prints() -> None:
    for path in (ROOT / "src" / "pingroom").glob("*.py"):
        source = path.read_text()
        assert "import logging" not in source, path.name
        assert not re.search(r"^\s*print\(", source, re.M), path.name
