from datetime import datetime, timezone

import pytest

from eddn_validator.classify import (
    classify,
    clean_strings,
    content_hash,
    game_branch,
    parse_schema_ref,
    parse_ts,
)


def envelope(schema="https://eddn.edcd.io/schemas/journal/1", **header_overrides):
    header = {
        "uploaderID": "0" * 40,
        "softwareName": "E:D Market Connector [Windows]",
        "softwareVersion": "6.1.2",
        "gameversion": "4.4.1.0",
        "gamebuild": "r332668/r0 ",
        "gatewayTimestamp": "2026-10-13T12:00:01.483147Z",
    }
    header.update(header_overrides)
    return {
        "$schemaRef": schema,
        "header": {k: v for k, v in header.items() if v is not ...},
        "message": {"timestamp": "2026-10-13T12:00:00Z", "event": "FSDJump",
                    "StarSystem": "Sol", "SystemAddress": 10477373803, "StarPos": [0.0, 0.0, 0.0]},
    }


@pytest.mark.parametrize("value, expected", [
    ("2026-10-13T12:00:00Z", datetime(2026, 10, 13, 12, tzinfo=timezone.utc)),
    ("2026-10-13T12:00:00.123456789Z", datetime(2026, 10, 13, 12, 0, 0, 123456, timezone.utc)),
    ("2026-10-13T14:00:00+02:00", datetime(2026, 10, 13, 12, tzinfo=timezone.utc)),
    ("2026-10-13T12:00:00", datetime(2026, 10, 13, 12, tzinfo=timezone.utc)),
    ("2026-10-13T23:59:60Z", None),
    ("yesterday", None),
    (None, None),
    (12345, None),
])
def test_parse_ts(value, expected):
    assert parse_ts(value) == expected


@pytest.mark.parametrize("ref, name, is_test", [
    ("https://eddn.edcd.io/schemas/journal/1", "journal/1", False),
    ("https://eddn.edcd.io/schemas/commodity/3/test", "commodity/3", True),
    ("https://eddn.edcd.io/schemas/fcmaterials_capi/1", "fcmaterials_capi/1", False),
    ("http://example.com/other/test", "http://example.com/other/test", True),
    (None, "unknown", False),
])
def test_parse_schema_ref(ref, name, is_test):
    _, schema_name, test = parse_schema_ref(ref)
    assert (schema_name, test) == (name, is_test)


@pytest.mark.parametrize("gv, branch", [
    ("4.4.1.0", "live"),
    ("4.0.0.1904", "live"),
    ("3.8.0.407", "legacy"),
    ("CAPI-Live-market", "live"),
    ("CAPI-Legacy-shipyard", "legacy"),
    ("CAPI-market", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    (4, "unknown"),
])
def test_game_branch(gv, branch):
    assert game_branch(gv) == branch


def test_classify_journal_event():
    c = classify(envelope())
    assert c.schema_name == "journal/1"
    assert c.event_type == "journal/1:FSDJump"
    assert c.branch == "live"
    assert c.source == "journal"
    assert c.game_build == "r332668/r0 "  # trailing space preserved
    assert c.msg_ts == datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert c.gw_ts.microsecond == 483147


def test_classify_capi_commodity_test_schema():
    env = envelope("https://eddn.edcd.io/schemas/commodity/3/test", gameversion="CAPI-Live-market")
    env["message"] = {"timestamp": "2026-10-13T12:00:00Z", "systemName": "Sol"}
    c = classify(env)
    assert (c.event_type, c.is_test, c.source, c.event) == ("commodity/3", True, "capi", None)


def test_classify_missing_gameversion_is_none_not_empty():
    c = classify(envelope(gameversion=..., gamebuild=...))
    assert c.game_version is None and c.game_build is None and c.branch == "unknown"
    assert classify(envelope(gameversion="")).game_version == ""


def test_classify_hostile_header_values():
    c = classify(envelope(softwareName="x" * 1000, softwareVersion=["1", "2"]))
    assert len(c.software_name) == 128
    assert c.software_version == '["1", "2"]'


def test_classify_garbage_envelope():
    c = classify({"$schemaRef": 7, "header": "nope", "message": []})
    assert c.software_name == "" and c.event_type == "7" and c.msg_ts is None


def test_content_hash_ignores_key_order_but_not_values():
    a = content_hash("s", {"a": 1, "b": 2})
    assert a == content_hash("s", {"b": 2, "a": 1})
    assert a != content_hash("s", {"a": 1, "b": 3})
    assert a != content_hash("s/test", {"a": 1, "b": 2})


def test_clean_strings():
    assert clean_strings({"a\x00": ["x\x00y", "\ud800z", 1]}) == {"a": ["xy", "?z", 1]}
