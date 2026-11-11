"""``ping_action``, ``link_ping``, ``location_ping`` and ``extract_location_ping``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from pingroom import PingRoomError, extract_location_ping, link_ping, location_ping, ping_action
from pingroom._ping_actions import (
    clean_ping_action_label,
    ping_action_body_problem,
    ping_action_label_looks_like_address,
    ping_action_url_verdict,
)

# --- ping_action ------------------------------------------------------------


def test_the_builders_make_the_wire_objects_data_actions_takes() -> None:
    assert ping_action.open_url("Open dashboard", "https://grafana.example.com/d/disk") == {
        "label": "Open dashboard",
        "type": "open_url",
        "url": "https://grafana.example.com/d/disk",
    }
    assert ping_action.webhook(
        "Clean up now",
        "https://ops.example.com/cleanup",
        id="clean",
        style="danger",
        body={"host": "db-2"},
    ) == {
        "label": "Clean up now",
        "type": "webhook",
        "url": "https://ops.example.com/cleanup",
        "id": "clean",
        "style": "danger",
        "body": {"host": "db-2"},
    }
    # The label every recipient sees: NFC, spaces collapsed, trimmed.
    assert ping_action.open_url("  Open  it ", "https://example.com/")["label"] == "Open it"


def refused(error: PingRoomError, pattern: str) -> bool:
    assert error.code == "invalid_data"
    assert error.status == 0
    assert re.search(pattern, error.message), error.message
    return True


@pytest.mark.parametrize(
    "call,pattern",
    [
        (
            lambda: ping_action.open_url("paypal.com", "https://evil.example.com/"),
            "web address, domain name or IP",
        ),
        (
            lambda: ping_action.open_url("\U0001f44d Pong Back", "https://example.com/"),
            "PingRoom's own buttons",
        ),
        (
            lambda: ping_action.open_url(
                "Pay \N{RIGHT-TO-LEFT OVERRIDE}lapyap", "https://example.com/"
            ),
            "invisible formatting",
        ),
        (lambda: ping_action.open_url("Go", "http://example.com/"), r"https://"),
        (
            lambda: ping_action.open_url("Go", "https://paypal.com@evil.example.com/"),
            "plain hostname",
        ),
        (lambda: ping_action.open_url("Go", "https://pаypal.com/"), "punycode"),
        (lambda: ping_action.open_url("Go", "https://127.0.0.1/"), "public hostname"),
        (lambda: ping_action.webhook("Run", "https://ops.example.com:8443/"), "cannot name a port"),
        (lambda: ping_action.webhook("Run", "https://db.internal/"), "public hostname"),
        (lambda: ping_action.webhook("Run", "https://ops.example.com/", body=["x"]), "JSON object"),  # type: ignore[arg-type]
        (
            lambda: ping_action.webhook("Run", "https://ops.example.com/", body={"x": "é" * 200}),
            "1024 bytes",
        ),
        (lambda: ping_action.open_url("Go", "https://example.com/", id="Bad Id"), "button id"),
        (lambda: ping_action.open_url("Go", "https://example.com/", style="red"), "style"),
    ],
)
def test_a_button_the_server_would_refuse_fails_before_any_request(call: Any, pattern: str) -> None:
    with pytest.raises(PingRoomError) as caught:
        call()
    assert refused(caught.value, pattern)
    assert caught.value.message.startswith("ping_action: ")


def test_open_url_takes_no_body() -> None:
    with pytest.raises(TypeError):
        ping_action.open_url("Go", "https://example.com/", body={"x": 1})  # type: ignore[call-arg]


def test_a_webhook_button_body_follows_the_php_rules() -> None:
    assert ping_action_body_problem({}) is None
    assert ping_action_body_problem({"a": 1}) is None
    # An object keyed "0", "1", ... is a list to the server.
    assert (
        ping_action_body_problem({"0": "a", "1": "b"})
        == "A webhook button body must be a JSON object."
    )
    assert ping_action_body_problem({str(i): 1 for i in range(11)}) is not None
    assert ping_action_body_problem({f"k{i}": 1 for i in range(10)}) is None
    assert ping_action_body_problem({"a": "/" * 513}) is not None  # '/' is two bytes
    assert ping_action_body_problem({"a": "/" * 508}) is None
    assert (
        ping_action_body_problem({"a": float("nan")})
        == "A webhook button body must be valid UTF-8 JSON."
    )


def test_the_url_rules_cover_ports_punycode_and_the_second_reading() -> None:
    ok = ping_action_url_verdict("https://Grafana.Example.com:3000/d?x=1#y", "open_url")
    assert (ok.error, ok.host) == (None, "grafana.example.com")
    assert ping_action_url_verdict("https://xn--bcher-kva.ch/", "open_url").error is None
    # The server does not decode punycode, and neither does the TypeScript SDK.
    assert ping_action_url_verdict("https://xn--a.com/", "open_url").error is None
    assert (
        ping_action_url_verdict("https://example.com:0/", "open_url").error
        == "A button url has an invalid port."
    )
    assert (
        ping_action_url_verdict("https://example.com:65536/", "open_url").error
        == "A button url has an invalid port."
    )
    assert ping_action_url_verdict("https://example.com:65535/", "open_url").error is None
    assert ping_action_url_verdict("https://" + "a" * 63 + ".com/", "open_url").error is None
    assert ping_action_url_verdict("https://" + "a" * 64 + ".com/", "open_url").error is not None
    assert (
        ping_action_url_verdict("https://example.com/" + "a" * 2100, "open_url").error is not None
    )
    assert ping_action_url_verdict("https://-a.example.com/", "open_url").error is not None
    assert ping_action_url_verdict("https://a-.example.com/", "open_url").error is not None
    assert ping_action_url_verdict("https://example.c/", "open_url").error is not None
    assert ping_action_url_verdict("https://exa mple.com/", "open_url").error is not None
    assert ping_action_url_verdict(None, "open_url").error == "A button needs a url."


def test_label_cleaning_edge_cases() -> None:
    assert (
        clean_ping_action_label("a\N{NO-BREAK SPACE}\N{EM SPACE}b").label == "a b"
    )  # space separators collapse
    assert clean_ping_action_label("e\N{COMBINING ACUTE ACCENT}").label == "é"  # NFC
    assert (
        clean_ping_action_label(
            "\U0001f468\N{ZERO WIDTH JOINER}\U0001f469\N{ZERO WIDTH JOINER}\U0001f467 Family"
        ).error
        is None
    )
    assert clean_ping_action_label("a\N{ZERO WIDTH SPACE}b").error is not None  # zero-width space
    assert clean_ping_action_label("a\ue000b").error is not None  # private use
    assert clean_ping_action_label("a\N{LINE SEPARATOR}b").error is not None  # line separator
    assert clean_ping_action_label("a\ud800b").error == "A button needs a label."  # lone surrogate
    assert clean_ping_action_label(5).error == "A button needs a label."
    assert (
        clean_ping_action_label("\U0001f3f4\U000e0067\U000e0062\U000e007f").error is None
    )  # tag block


@pytest.mark.parametrize(
    "label,expected",
    [
        ("example.com", True),
        ("see www.x", True),
        ("https://x", True),
        ("a.b.co", True),
        ("10.0.0.5", True),
        ("١٠.٠.٠.٥", True),  # Arabic-Indic digits, as PHP's /u matches
        ("ｐａｙｐａｌ．ｃｏｍ", True),  # fullwidth
        ("login。bank。co", True),  # ideographic full stops
        ("Open 3.5", False),
        ("v2.0", False),
        ("Ship it.", False),
        ("e.g. this", False),
        ("Fix: a.b", False),
        ("Hello", False),
        ("1.2.3", False),
        ("Done.", False),
    ],
)
def test_the_address_shape_check(label: str, expected: bool) -> None:
    assert ping_action_label_looks_like_address(label) is expected


# --- link_ping --------------------------------------------------------------


def test_link_ping_builds_the_data_fragment() -> None:
    assert link_ping("https://ci.example.com/b/512", button_label="Open build") == {
        "url": "https://ci.example.com/b/512",
        "button_label": "Open build",
    }
    assert link_ping("https://example.com") == {"url": "https://example.com"}
    assert link_ping("http://localhost:8000/x") == {"url": "http://localhost:8000/x"}


@pytest.mark.parametrize(
    "url,pattern",
    [
        ("/relative", "not a valid URL"),
        ("example.com/x", "not a valid URL"),
        ("https://", "not a valid URL"),
        ("ftp://example.com/x", r"absolute http\(s\)"),
        ("mailto:a@b.c", r"absolute http\(s\)"),
        ("https://example.com/" + "x" * 2048, "at most 2048"),
    ],
)
def test_link_ping_rejects_what_the_server_rejects(url: str, pattern: str) -> None:
    with pytest.raises(ValueError, match=pattern):
        link_ping(url)


def test_link_ping_rejects_an_over_long_label_and_wrong_types() -> None:
    with pytest.raises(ValueError, match="at most 26"):
        link_ping("https://example.com", button_label="x" * 27)
    with pytest.raises(TypeError):
        link_ping(5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        link_ping("https://example.com", button_label=5)  # type: ignore[arg-type]


def test_link_ping_counts_characters_like_the_server_not_utf16_units() -> None:
    label = "\U0001f600" * 14
    assert link_ping("https://example.com", button_label=label) == {
        "url": "https://example.com",
        "button_label": label,
    }
    with pytest.raises(ValueError, match="at most 26"):
        link_ping("https://example.com", button_label="\U0001f600" * 27)
    url = "https://example.com/" + "é" * 2028
    assert len(url) == 2048
    assert link_ping(url)["url"] == url


# --- location_ping ----------------------------------------------------------


def test_location_ping_builds_the_reserved_fragment() -> None:
    assert location_ping(25.2048, 55.2708, label="Dubai Mall", address="Downtown Dubai") == {
        "location": {
            "latitude": 25.2048,
            "longitude": 55.2708,
            "label": "Dubai Mall",
            "address": "Downtown Dubai",
        }
    }


def test_location_ping_accepts_inclusive_boundaries_and_omits_absent_strings() -> None:
    assert location_ping(-90, 180) == {"location": {"latitude": -90, "longitude": 180}}
    assert location_ping(90, -180) == {"location": {"latitude": 90, "longitude": -180}}


@pytest.mark.parametrize(
    "latitude", ["25.2", float("nan"), float("inf"), float("-inf"), None, True]
)
def test_location_ping_requires_true_finite_json_numbers(latitude: Any) -> None:
    with pytest.raises(TypeError, match="latitude must be a finite JSON number"):
        location_ping(latitude, 55.3)
    with pytest.raises(TypeError, match="longitude must be a finite JSON number"):
        location_ping(25.2, "55.3")  # type: ignore[arg-type]


def test_location_ping_rejects_coordinates_outside_their_ranges() -> None:
    with pytest.raises(ValueError, match="latitude must be between -90 and 90"):
        location_ping(90.000001, 0)
    with pytest.raises(ValueError, match="longitude must be between -180 and 180"):
        location_ping(0, -180.000001)


def test_location_ping_caps_label_and_address_in_unicode_characters() -> None:
    location_ping(0, 0, label="\U0001f4cd" * 100, address="\U0001f4cd" * 255)
    with pytest.raises(ValueError, match="label must be at most 100 characters"):
        location_ping(0, 0, label="\U0001f4cd" * 101)
    with pytest.raises(ValueError, match="address must be at most 255 characters"):
        location_ping(0, 0, address="\U0001f4cd" * 256)
    with pytest.raises(TypeError, match="label must be a string"):
        location_ping(0, 0, label=5)  # type: ignore[arg-type]


def test_extract_location_ping_returns_a_validated_copy_or_none() -> None:
    source = {
        "location": {"latitude": 51.5072, "longitude": -0.1276, "label": "London"},
        "sibling": True,
    }
    extracted = extract_location_ping(source)
    assert extracted == {"latitude": 51.5072, "longitude": -0.1276, "label": "London"}
    assert extracted is not source["location"]
    assert extract_location_ping({}) is None
    assert extract_location_ping({"location": {"latitude": "51.5", "longitude": -0.1}}) is None
    assert (
        extract_location_ping({"location": {"latitude": 1, "longitude": 1, "label": None}}) is None
    )
    assert extract_location_ping(None) is None
    assert extract_location_ping([]) is None
