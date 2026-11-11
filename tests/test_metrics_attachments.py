"""Room metrics and attachments: exact requests, local checks, refusals."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from conftest import Harness, Recorder, raw_path, reply, static
from pingroom import METRIC_ERROR_CODES, PingRoomError
from pingroom._metrics import MAX_METRICS_PER_ROOM, METRIC_FORMATS, normalize_metric_key


def stored(**overrides: Any) -> Dict[str, Any]:
    return {
        "key": "mrr",
        "label": "MRR",
        "format": "currency",
        "unit": "USD",
        "value": 12400,
        "value_updated_at": "2026-11-11T08:12:30.000000Z",
        **overrides,
    }


def invalid(error: PingRoomError) -> bool:
    return error.code == "invalid_request" and error.status == 0


def test_the_limits_mirror_the_server() -> None:
    assert MAX_METRICS_PER_ROOM == 8
    assert list(METRIC_FORMATS) == ["number", "currency", "percent", "unit", "text"]
    assert list(METRIC_ERROR_CODES) == [
        "metrics_limit_reached",
        "metric_not_found",
        "metric_exists",
    ]


def test_list_returns_the_metrics_and_the_limit(harness: Harness) -> None:
    body = {
        "metrics": [stored(), stored(key="status", format="text", unit=None, value="All clear")],
        "limit": 8,
    }
    rec = Recorder(routes={"GET /api/agent/rooms/ab12/metrics": static(body)})
    client = harness.client(rec, token="t")
    result = harness.run(client, lambda c: c.metrics.list("ab12"))
    assert result["limit"] == 8
    assert [m["key"] for m in result["metrics"]] == ["mrr", "status"]
    assert result["metrics"][0]["value"] == 12400
    assert rec.last.headers["authorization"] == "Bearer t"
    assert rec.last.method == "GET"


def test_set_puts_the_lowercased_key_with_only_the_fields_given(harness: Harness) -> None:
    rec = Recorder(
        fallback=lambda request: reply(stored(**json.loads(request.content)), status=201)
    )
    client = harness.client(rec)
    created = harness.run(
        client,
        lambda c: c.metrics.set("ab12", " MRR ", 12400, label="MRR", format="currency", unit="usd"),
    )
    assert created["key"] == "mrr"
    assert rec.last.method == "PUT"
    assert rec.last.url.path == "/api/agent/rooms/ab12/metrics/mrr"
    assert rec.body() == {"value": 12400, "label": "MRR", "format": "currency", "unit": "usd"}
    harness.run(client, lambda c: c.metrics.set("ab12", "mrr", 12650, label=None))
    assert rec.body() == {"value": 12650}


@pytest.mark.parametrize(
    "value,kwargs",
    [
        (0, {}),
        (-3.5, {}),
        (1e15, {}),
        (-1e15, {}),
        (10**15, {}),
        ("12.50", {}),
        ("-0.5", {"format": "percent"}),
        ("All clear", {}),
        ("é" * 24, {"format": "text"}),
        ("42", {"format": "text"}),
        ("NaN", {"format": "text"}),
        (240, {"format": "unit", "unit": "abcdefgh"}),
    ],
)
def test_set_accepts_every_value_the_server_accepts(
    harness: Harness, value: Any, kwargs: Dict[str, Any]
) -> None:
    rec = Recorder(fallback=lambda request: reply(stored(**json.loads(request.content))))
    client = harness.client(rec)
    harness.run(client, lambda c: c.metrics.set("ab12", "k", value, **kwargs))
    assert rec.body()["value"] == value


REFUSALS: List[Tuple[Any, Any, Dict[str, Any], str]] = [
    ("", 1, {}, "`key` must be 1–40"),
    ("bad key", 1, {}, "`key` must be 1–40"),
    ("a.b", 1, {}, "`key` must be 1–40"),
    ("-a", 1, {}, "`key` must be 1–40"),
    ("a" * 41, 1, {}, "`key` must be 1–40"),
    (5, 1, {}, "`key` must be a string"),
    ("k", None, {}, "`value` is required"),
    ("k", True, {}, "`value` must be a number or a string"),
    ("k", [1], {}, "`value` must be a number or a string"),
    ("k", {}, {}, "`value` must be a number or a string"),
    ("k", float("nan"), {}, "finite number"),
    ("k", float("inf"), {}, "finite number"),
    ("k", float("-inf"), {"format": "number"}, "finite number"),
    ("k", 1e15 + 1e6, {}, "absolute value of at most 1000000000000000"),
    ("k", 10**400, {}, "absolute value of at most 1000000000000000"),
    ("k", "abc", {"format": "number"}, "`value` must be a number for the number format"),
    ("k", "1e5", {"format": "number"}, "must be a number"),
    ("k", "NaN", {"format": "percent"}, "must be a number"),
    ("k", "0x10", {"format": "unit", "unit": "ms"}, "must be a number"),
    ("k", "x" * 25, {}, "1–24 characters"),
    ("k", "a\nb", {}, "control characters"),
    ("k", 5, {"format": "text"}, "must be a string for the text format"),
    ("k", "x" * 25, {"format": "text"}, "1–24 characters for the text format"),
    ("k", 1, {"format": "money"}, "`format` must be one of: number, currency, percent, unit, text"),
    ("k", 1, {"label": ""}, "`label` must be 1–24"),
    ("k", 1, {"label": "x" * 25}, "`label` must be 1–24"),
    ("k", 1, {"label": "a\tb"}, "`label` must not contain control characters"),
    ("k", 1, {"label": 5}, "`label` must be a string"),
    ("k", 1, {"format": "number", "unit": "ms"}, "only allowed with the currency and unit formats"),
    ("k", "a", {"format": "text", "unit": "ms"}, "only allowed with the currency and unit formats"),
    ("k", 1, {"format": "percent", "unit": "%"}, "only allowed with the currency and unit formats"),
    ("k", 1, {"format": "currency", "unit": "US"}, "ISO 4217 code"),
    ("k", 1, {"format": "currency", "unit": "USDX"}, "ISO 4217 code"),
    ("k", 1, {"format": "currency", "unit": "123"}, "ISO 4217 code"),
    ("k", 1, {"format": "unit", "unit": "abcdefghi"}, "`unit` must be 1–8"),
    ("k", 1, {"format": "unit", "unit": ""}, "`unit` must be 1–8"),
]


@pytest.mark.parametrize("key,value,kwargs,expected", REFUSALS)
def test_set_refuses_a_write_the_server_would_refuse_before_any_request(
    harness: Harness, key: Any, value: Any, kwargs: Dict[str, Any], expected: str
) -> None:
    rec = Recorder(fallback=static({}))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.set("ab12", key, value, **kwargs))
    assert invalid(caught.value)
    assert expected in caught.value.message
    assert rec.requests == []


def test_a_missing_value_is_refused_whatever_the_other_fields(harness: Harness) -> None:
    client = harness.client(Recorder(fallback=static({})))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.set("ab12", "k", None, label="x"))
    assert "`value` is required" in caught.value.message


def test_label_unit_and_text_limits_count_code_points(harness: Harness) -> None:
    rec = Recorder(fallback=lambda request: reply(stored(**json.loads(request.content))))
    client = harness.client(rec)
    harness.run(client, lambda c: c.metrics.set("ab12", "k", 1, label="\U0001f600" * 24))
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.metrics.set("ab12", "k", 1, label="\U0001f600" * 25))
    harness.run(client, lambda c: c.metrics.set("ab12", "k", "\U0001f600" * 24, format="text"))
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.metrics.set("ab12", "k", "\U0001f600" * 25, format="text"))


def test_a_currency_code_is_checked_for_its_shape_only(harness: Harness) -> None:
    rec = Recorder(fallback=lambda request: reply(stored(**json.loads(request.content))))
    client = harness.client(rec)
    # ZZZ is not an ISO 4217 code and the server will refuse it, but a code ISO
    # adds later must not be refused here first.
    harness.run(client, lambda c: c.metrics.set("ab12", "k", 1, format="currency", unit="zzz"))
    assert rec.body()["unit"] == "zzz"


def test_delete_sends_delete_for_the_lowercased_key_and_resolves_with_nothing(
    harness: Harness,
) -> None:
    rec = Recorder(
        routes={
            "DELETE /api/agent/rooms/ab12/metrics/mrr": static(
                {"message": "Metric deleted successfully"}
            ),
            "DELETE /api/agent/rooms/ab12/metrics/gone": static(
                {"message": "Metric not found", "code": "metric_not_found"}, 404
            ),
        }
    )
    client = harness.client(rec)
    assert harness.run(client, lambda c: c.metrics.delete("ab12", "MRR")) is None
    assert rec.last.method == "DELETE"
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.delete("ab12", "gone"))
    assert (caught.value.status, caught.value.code) == (404, "metric_not_found")
    for bad in ("..", "bad key"):
        with pytest.raises(PingRoomError) as caught:
            harness.run(client, lambda c, k=bad: c.metrics.delete("ab12", k))
        assert invalid(caught.value)
    assert len(rec.requests) == 2


def test_a_refusal_carries_its_code_status_and_message(harness: Harness) -> None:
    rec = Recorder(
        routes={
            "PUT /api/agent/rooms/ab12/metrics/full": static(
                {
                    "message": "Maximum of 8 metrics per room reached.",
                    "code": "metrics_limit_reached",
                },
                422,
            ),
            "PUT /api/agent/rooms/ab12/metrics/off": static(
                {
                    "message": "Room metrics are turned off.",
                    "code": "feature_disabled",
                    "feature": "rooms.metrics",
                },
                403,
            ),
            "PUT /api/agent/rooms/ab12/metrics/scope": static(
                {
                    "code": "insufficient_scope",
                    "message": "This credential lacks pingroom:metrics:write",
                },
                403,
            ),
        }
    )
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.set("ab12", "full", 1))
    assert caught.value.code in METRIC_ERROR_CODES and caught.value.status == 422
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.set("ab12", "off", 1))
    assert (caught.value.code, caught.value.status) == ("feature_disabled", 403)
    assert caught.value.body["feature"] == "rooms.metrics"
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.metrics.set("ab12", "scope", 1))
    assert caught.value.code == "insufficient_scope"


def test_normalize_metric_key_trims_lowercases_ascii_only_and_checks_the_stored_shape() -> None:
    assert normalize_metric_key("  Open-Tickets_2 ") == "open-tickets_2"
    for bad in ("É", "İ", None):
        with pytest.raises(PingRoomError):
            normalize_metric_key(bad)


# --- attachments -----------------------------------------------------------

ATTACHMENT = {
    "id": "att_1",
    "filename": "brief.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 5,
    "created_at": "2026-08-10T08:00:00Z",
}


def test_upload_posts_multipart_and_lets_httpx_own_the_boundary(harness: Harness) -> None:
    rec = Recorder(routes={"POST /api/agent/attachments": static({"attachment": ATTACHMENT}, 201)})
    client = harness.client(rec, token="tok_abc")
    attachment = harness.run(
        client,
        lambda c: c.attachments.upload(b"%PDF-", "brief.pdf", content_type="application/pdf"),
    )
    assert attachment == ATTACHMENT
    request = rec.last
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert request.headers["authorization"] == "Bearer tok_abc"
    body = request.content
    boundary = content_type.split("boundary=")[1].encode()
    assert b'name="file"; filename="brief.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"\r\n\r\n%PDF-\r\n--" + boundary in body


def test_upload_accepts_text_and_defaults_the_part_type_to_octet_stream(harness: Harness) -> None:
    rec = Recorder(fallback=static({"attachment": ATTACHMENT}, 201))
    client = harness.client(rec)
    harness.run(client, lambda c: c.attachments.upload("notes", "notes.txt"))
    assert b"Content-Type: application/octet-stream" in rec.last.content
    assert b"\r\n\r\nnotes\r\n--" in rec.last.content
    harness.run(client, lambda c: c.attachments.upload(memoryview(b"abc"), "a.txt"))
    harness.run(client, lambda c: c.attachments.upload(bytearray(b"abc"), "a.txt"))


def test_upload_refuses_over_5_mib_and_empty_names_before_any_request(harness: Harness) -> None:
    rec = Recorder(fallback=static({"attachment": ATTACHMENT}, 201))
    client = harness.client(rec)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload(b"x" * (5 * 1024 * 1024 + 1), "big.txt"))
    assert (caught.value.code, caught.value.status) == ("attachment_too_large", 413)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload(b"x", " "))
    assert invalid(caught.value)
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload(12345, "a.txt"))
    assert invalid(caught.value)
    assert rec.requests == []
    harness.run(client, lambda c: c.attachments.upload(b"x" * (5 * 1024 * 1024), "ok.txt"))
    assert len(rec.requests) == 1


def test_a_402_upload_surfaces_the_pro_gate(harness: Harness) -> None:
    client = harness.client(
        static(
            {
                "error": "pro_required",
                "code": "pro_required",
                "message": "Ping attachments are a Pro feature.",
            },
            402,
        )
    )
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload("notes", "notes.txt"))
    assert (caught.value.status, caught.value.code) == (402, "pro_required")


def test_an_upload_answer_without_an_attachment_is_invalid_response(harness: Harness) -> None:
    client = harness.client(static({"unexpected": True}, 201))
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.upload("notes", "notes.txt"))
    assert caught.value.code == "invalid_response"


def test_manifest_returns_a_zip_listing_and_none_for_a_non_archive(harness: Harness) -> None:
    manifest = {
        "entries": [
            {"name": "src", "size_bytes": None, "is_directory": True},
            {"name": "src/index.js", "size_bytes": 300, "is_directory": False},
        ],
        "total_entries": 2,
        "truncated": False,
        "total_uncompressed_bytes": 300,
    }
    rec = Recorder(
        routes={
            "GET /api/agent/attachments/att_zip/manifest": static({"manifest": manifest}),
            "GET /api/agent/attachments/att_pdf/manifest": static({"manifest": None}),
        }
    )
    client = harness.client(rec, token="tok_abc")
    result = harness.run(client, lambda c: c.attachments.manifest("att_zip"))
    assert result == manifest
    assert harness.run(client, lambda c: c.attachments.manifest("att_pdf")) is None
    assert len(rec.requests) == 2


def test_content_returns_raw_bytes_but_still_raises_on_a_refusal(harness: Harness) -> None:
    rec = Recorder(
        routes={
            "GET /api/agent/attachments/att_1/content": lambda r: httpx.Response(
                200,
                content=b"%PDF-1.7 \xff\xfe binary",
                headers={"Content-Type": "application/pdf"},
            ),
            "GET /api/agent/attachments/att_gone/content": static({"message": "Not found"}, 404),
        }
    )
    client = harness.client(rec)
    result = harness.run(client, lambda c: c.attachments.content("att_1"))
    assert result == {"content": b"%PDF-1.7 \xff\xfe binary", "content_type": "application/pdf"}
    assert rec.last.headers["accept"] == "*/*"
    with pytest.raises(PingRoomError) as caught:
        harness.run(client, lambda c: c.attachments.content("att_gone"))
    assert caught.value.status == 404
    # The undecoded path never parses a success as JSON.
    client = harness.client(lambda request: reply(text="not json at all"))
    assert (
        harness.run(client, lambda c: c.attachments.content("att_1"))["content"]
        == b"not json at all"
    )


def test_delete_discards_an_unattached_upload(harness: Harness) -> None:
    rec = Recorder(fallback=static({"message": "Attachment deleted"}))
    client = harness.client(rec)
    assert harness.run(client, lambda c: c.attachments.delete("att/1")) is None
    assert rec.last.method == "DELETE"
    assert raw_path(rec.last) == b"/api/agent/attachments/att%2F1"
    with pytest.raises(PingRoomError):
        harness.run(client, lambda c: c.attachments.delete(" "))
