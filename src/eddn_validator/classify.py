"""Classify a relayed EDDN envelope into the columns stored alongside it.

Everything in an EDDN message is sender-controlled, so every value is
type-checked and length-capped here before it reaches an indexed column.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_NAME = 128
MAX_VERSION = 64
MAX_SCHEMA_REF = 200
MAX_EVENT = 64
MAX_GAME = 64

SCHEMA_RE = re.compile(
    r"^https://eddn\.edcd\.io/schemas/(?P<name>[a-z_]+)/(?P<ver>\d+)(?P<test>/test)?$"
)
TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:?\d{2})?$"
)
MAJOR_RE = re.compile(r"^(\d{1,9})\.")   # bounded: int() refuses over 4300 digits


@dataclass(frozen=True, slots=True)
class Classified:
    software_name: str
    software_version: str
    schema_ref: str
    schema_name: str
    is_test: bool
    event: str | None
    event_type: str
    branch: str
    source: str
    game_version: str | None
    game_build: str | None
    msg_ts: datetime | None
    gw_ts: datetime | None
    content_hash: bytes


def parse_ts(value: object) -> datetime | None:
    """Parse an EDDN timestamp to an aware UTC datetime, or None.

    Accepts any number of fractional digits (truncated to microseconds) and
    treats a missing offset as UTC.
    """
    if not isinstance(value, str):
        return None
    m = TS_RE.match(value.strip())
    if not m:
        return None
    date, time_, frac, tz = m.groups()
    frac = (frac or "")[:6].ljust(6, "0")
    if tz is None or tz in ("Z", "z"):
        tz = "+00:00"
    elif ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    try:
        return datetime.fromisoformat(f"{date}T{time_}.{frac}{tz}").astimezone(timezone.utc)
    except (ValueError, OverflowError):   # e.g. year 0001 with a +01:00 offset
        return None


def _text(value: object, limit: int) -> str | None:
    """A column-safe string: no NULs or lone surrogates (they cannot be stored), capped."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value)
    value = value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    return value[:limit]


def parse_schema_ref(ref: object) -> tuple[str, str, bool]:
    """Return (schema_ref, schema_name, is_test)."""
    ref = _text(ref, MAX_SCHEMA_REF) or ""
    m = SCHEMA_RE.match(ref)
    if m:
        return ref, f"{m['name']}/{m['ver']}", m["test"] is not None
    return ref, ref or "unknown", ref.endswith("/test")


def game_branch(game_version: object) -> str:
    """live / legacy / unknown, from header.gameversion (docs/DESIGN.md §2.8)."""
    if not isinstance(game_version, str):
        return "unknown"
    if game_version.startswith("CAPI-Live-"):
        return "live"
    if game_version.startswith("CAPI-Legacy-"):
        return "legacy"
    m = MAJOR_RE.match(game_version.strip())
    if m:
        major = int(m.group(1))
        if major >= 4:
            return "live"
        if major == 3:
            return "legacy"
    return "unknown"


def content_hash(schema_ref: str, message: object) -> bytes:
    """Hash of the schema and message body, used to spot duplicate sends."""
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{schema_ref}\n{canonical}".encode()).digest()


def classify(envelope: dict) -> Classified:
    header = envelope.get("header")
    if not isinstance(header, dict):
        header = {}
    message = envelope.get("message")
    if not isinstance(message, dict):
        message = {}

    schema_ref, schema_name, is_test = parse_schema_ref(envelope.get("$schemaRef"))
    event = message.get("event")
    event = event[:MAX_EVENT] if isinstance(event, str) else None
    if schema_name == "journal/1":
        event_type = f"journal/1:{event or '?'}"
    else:
        event_type = schema_name

    game_version = _text(header.get("gameversion"), MAX_GAME) if "gameversion" in header else None
    game_build = _text(header.get("gamebuild"), MAX_GAME) if "gamebuild" in header else None

    return Classified(
        software_name=_text(header.get("softwareName"), MAX_NAME) or "",
        software_version=_text(header.get("softwareVersion"), MAX_VERSION) or "",
        schema_ref=schema_ref,
        schema_name=schema_name,
        is_test=is_test,
        event=event,
        event_type=event_type,
        branch=game_branch(header.get("gameversion")),
        source="capi" if isinstance(game_version, str) and game_version.startswith("CAPI-") else "journal",
        game_version=game_version,
        game_build=game_build,
        msg_ts=parse_ts(message.get("timestamp")),
        gw_ts=parse_ts(header.get("gatewayTimestamp")),
        content_hash=content_hash(schema_ref, message),
    )


def clean_strings(value: object) -> object:
    """Remove NUL characters and lone surrogates, which jsonb cannot store."""
    if isinstance(value, str):
        return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {clean_strings(k): clean_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_strings(v) for v in value]
    return value
