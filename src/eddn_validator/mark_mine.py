"""Mark ED Outrider messages as the owner's own (or another player's) using Outrider's outbox.

Runs on the PC whose Outrider sends to EDDN. That Outrider writes each message
to its outbox (upload_queue in ed_outrider.sqlite) before sending it, and EDDN
relays the message body unchanged (only the header's uploaderID becomes a hash).
So a relayed message is the owner's when the outbox holds the same body for the
same schema. The outbox keeps sent rows for about a week: messages older than
its oldest sent row cannot be told and stay unknown (NULL).

Same matching as the earlier standalone listener script.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from pathlib import Path

from . import db
from .classify import parse_ts
from .config import Settings
from .outrider import OUTRIDER

log = logging.getLogger(__name__)


def _norm(x: object) -> object:
    """Whole-number floats as ints, so 3324.0 and 3324 compare equal."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def message_key(schema_ref: object, message: object) -> tuple[object, str]:
    return schema_ref, json.dumps(_norm(message), sort_keys=True)


def read_outbox(path: Path) -> tuple[set[tuple[object, str]], str | None]:
    """(keys of every EDDN message in the outbox, created time of the oldest sent one)."""
    outbox = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        oldest = outbox.execute("SELECT min(created) FROM upload_queue"
                                " WHERE service = 'eddn' AND state = 'sent'").fetchone()[0]
        keys = set()
        for (text,) in outbox.execute("SELECT message FROM upload_queue WHERE service = 'eddn'"):
            try:
                envelope = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(envelope, dict):
                keys.add(message_key(envelope.get("$schemaRef"), envelope.get("message")))
    finally:
        outbox.close()
    return keys, oldest


def mark_mine(settings: Settings, outbox_path: str) -> None:
    path = Path(outbox_path).expanduser()
    if not path.exists():
        raise SystemExit(f"outbox not found: {path}")
    # Only messages relayed before the outbox was read can be judged: one sent after it is
    # not in `keys` and would be marked another player's for good. Two minutes allow for the
    # relay delay and clock differences; newer rows wait for the next run.
    judged_until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    keys, oldest = read_outbox(path)
    oldest_ts = parse_ts(oldest)
    if oldest_ts is None:
        log.info("outbox has no sent EDDN messages; nothing can be marked")
        return

    with db.connect(settings, application_name="mark-mine") as conn:
        row = conn.execute("SELECT id FROM apps WHERE software_name = %s", (OUTRIDER,)).fetchone()
        if row is None:
            log.info("no %s messages stored yet", OUTRIDER)
            return
        rows = conn.execute(
            """
            SELECT id, gw_ts, payload->'$schemaRef', payload->'message'
              FROM messages
             WHERE app_id = %s AND mine IS NULL AND gw_ts >= %s AND gw_ts < %s
            """, (row[0], oldest_ts, judged_until)).fetchall()
        updates = [(message_key(ref, msg) in keys, mid, gw_ts) for mid, gw_ts, ref, msg in rows]
        if updates:
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany("UPDATE messages SET mine = %s WHERE id = %s AND gw_ts = %s", updates)
        unknown = conn.execute(
            "SELECT count(*) FROM messages WHERE app_id = %s AND mine IS NULL",
            (row[0],)).fetchone()[0]
    mine = sum(1 for m, _, _ in updates if m)
    log.info("marked %d messages: %d mine, %d other players (%d outbox entries, oldest sent %s);"
             " %d remain unknown", len(updates), mine, len(updates) - mine, len(keys), oldest,
             unknown)
