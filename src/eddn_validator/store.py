"""Decoding relayed envelopes and writing them to the messages table.

Shared by the live listener and the SQLite importer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import psycopg

from .classify import Classified, classify, clean_strings
from .config import ReferenceApps

log = logging.getLogger(__name__)

COPY_SQL = (
    "COPY messages (gw_ts, received_at, msg_ts, app_id, software_version, schema_ref,"
    " schema_name, is_test, event, event_type, branch, source, game_version, game_build,"
    " is_big3, content_hash, payload, mine) FROM STDIN"
)


@dataclass(slots=True)
class Decoded:
    received_at: datetime
    c: Classified
    ref_group: str | None
    payload: str
    mine: bool | None = None

    @property
    def gw_ts(self) -> datetime:
        return self.c.gw_ts or self.received_at


def decode(text: str, received_at: datetime, refs: ReferenceApps,
           mine: bool | None = None) -> Decoded:
    """Parse and classify one envelope. Raises ValueError if it is not a JSON object."""
    envelope = json.loads(text)
    if not isinstance(envelope, dict):
        raise ValueError("envelope is not a JSON object")
    if "\\u0000" in text:
        envelope = clean_strings(envelope)
        text = json.dumps(envelope)
    c = classify(envelope)
    return Decoded(received_at, c, refs.group_for(c.software_name), text, mine)


def store(conn: psycopg.Connection, batch: list[Decoded]) -> tuple[int, int]:
    """Write a batch in one transaction. Returns (written, unstorable)."""
    with conn.transaction():
        app_ids = _upsert_apps(conn, batch)
        _upsert_versions(conn, batch, app_ids)
        return copy_rows(conn, batch, app_ids)


def register_apps(conn: psycopg.Connection, batch: list[Decoded],
                  count: bool = True) -> dict[str, int]:
    """Upsert the batch's apps and versions in their own short transaction.

    For long-running writers (archive import): the apps rows are shared with the
    live listener, so their locks must not be held for the length of a big file.
    With count=False the message counts are left alone (the caller adds them with
    add_version_counts once its rows are committed, so a retried file isn't counted twice).
    """
    with conn.transaction():
        app_ids = _upsert_apps(conn, batch)
        _upsert_versions(conn, batch, app_ids, count)
    return app_ids


def version_counts(batch: list[Decoded], app_ids: dict[str, int]) -> dict[tuple[int, str], int]:
    out: dict[tuple[int, str], int] = {}
    for d in batch:
        key = (app_ids[d.c.software_name], d.c.software_version)
        out[key] = out.get(key, 0) + 1
    return out


def add_version_counts(conn: psycopg.Connection, counts: dict[tuple[int, str], int]) -> None:
    """Add (or, with negative numbers, remove) message counts per app version."""
    if not counts:
        return
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE app_versions SET msg_count = greatest(0, msg_count + %s)"
            " WHERE app_id = %s AND version = %s",
            [(n, app_id, version) for (app_id, version), n in sorted(counts.items()) if n])


def copy_rows(conn: psycopg.Connection, batch: list[Decoded],
              app_ids: dict[str, int]) -> tuple[int, int]:
    """COPY a batch inside the caller's transaction. Returns (written, unstorable)."""
    try:
        with conn.transaction(), conn.cursor().copy(COPY_SQL) as copy:
            for d in batch:
                copy.write_row(_row(d, app_ids))
        return len(batch), 0
    except (psycopg.DataError, UnicodeEncodeError) as exc:
        log.warning("batch rejected (%s); storing rows one at a time", exc)
        written = _store_one_by_one(conn, batch, app_ids)
        return written, len(batch) - written


def _store_one_by_one(conn: psycopg.Connection, batch: list[Decoded],
                      app_ids: dict[str, int]) -> int:
    written = 0
    for d in batch:
        error: Exception | None = None
        for attempt in (d, _cleaned(d)):
            try:
                with conn.transaction(), conn.cursor().copy(COPY_SQL) as copy:
                    copy.write_row(_row(attempt, app_ids))
                written += 1
                break
            except (psycopg.DataError, UnicodeEncodeError) as exc:
                error = exc
        else:
            log.warning("dropped unstorable message from %r: %s", d.c.software_name, error)
    return written


def _cleaned(d: Decoded) -> Decoded:
    payload = json.dumps(clean_strings(json.loads(d.payload)), ensure_ascii=False)
    return Decoded(d.received_at, d.c, d.ref_group, payload, d.mine)


def _row(d: Decoded, app_ids: dict[str, int]) -> tuple:
    c = d.c
    return (
        d.gw_ts, d.received_at, c.msg_ts, app_ids[c.software_name], c.software_version,
        c.schema_ref, c.schema_name, c.is_test, c.event, c.event_type, c.branch, c.source,
        c.game_version, c.game_build, d.ref_group is not None, c.content_hash, d.payload, d.mine,
    )


def _upsert_apps(conn: psycopg.Connection, batch: list[Decoded]) -> dict[str, int]:
    seen: dict[str, tuple[str | None, datetime, datetime]] = {}
    for d in batch:
        name = d.c.software_name
        group, first, last = seen.get(name, (d.ref_group, d.gw_ts, d.gw_ts))
        seen[name] = (group, min(first, d.gw_ts), max(last, d.gw_ts))
    # Existing apps are updated in place; only new names are inserted. (An INSERT ... ON
    # CONFLICT would draw a value from apps.id's sequence on every batch even when the row
    # exists, exhausting the 32-bit id within a couple of years.)
    ids = dict(conn.execute("SELECT software_name, id FROM apps WHERE software_name = ANY(%s)",
                            (sorted(seen),)).fetchall())
    # Sorted, so concurrent writers (listener, import workers) lock rows in one order.
    for name, (group, first, last) in sorted(seen.items()):
        if name in ids:
            conn.execute(
                """
                UPDATE apps SET ref_group = %s,
                       first_seen = LEAST(first_seen, %s), last_seen = GREATEST(last_seen, %s)
                 WHERE id = %s AND (ref_group IS DISTINCT FROM %s OR first_seen > %s OR last_seen < %s)
                """, (group, first, last, ids[name], group, first, last))
            continue
        row = conn.execute(
            """
            INSERT INTO apps (software_name, ref_group, first_seen, last_seen)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (software_name) DO UPDATE
               SET ref_group = EXCLUDED.ref_group,
                   first_seen = LEAST(apps.first_seen, EXCLUDED.first_seen),
                   last_seen = GREATEST(apps.last_seen, EXCLUDED.last_seen)
            RETURNING id
            """,
            (name, group, first, last),
        ).fetchone()
        ids[name] = row[0]
    return ids


def _upsert_versions(conn: psycopg.Connection, batch: list[Decoded],
                     app_ids: dict[str, int], count: bool = True) -> None:
    seen: dict[tuple[int, str], list] = {}
    for d in batch:
        key = (app_ids[d.c.software_name], d.c.software_version)
        entry = seen.setdefault(key, [d.gw_ts, d.gw_ts, 0])
        entry[0] = min(entry[0], d.gw_ts)
        entry[1] = max(entry[1], d.gw_ts)
        entry[2] += 1 if count else 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO app_versions (app_id, version, first_seen, last_seen, msg_count)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (app_id, version) DO UPDATE
               SET first_seen = LEAST(app_versions.first_seen, EXCLUDED.first_seen),
                   last_seen = GREATEST(app_versions.last_seen, EXCLUDED.last_seen),
                   msg_count = app_versions.msg_count + EXCLUDED.msg_count
            """,
            [(app_id, version, *values) for (app_id, version), values in sorted(seen.items())],
        )
