"""Import the SQLite file kept by the earlier standalone listener script.

Its messages table holds the whole relayed envelope in `message`, the local
receive time in `received` (UTC, "YYYY-MM-DD HH:MM:SS") and, for ED Outrider
messages, `mine` (1 own instance, 0 another player, NULL unknown). Rows are
re-classified exactly as the live listener would. Progress is recorded in
sqlite_imports, so re-running resumes rather than duplicating.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from . import db, partitions
from .classify import parse_ts
from .config import Settings
from .store import decode, store

log = logging.getLogger(__name__)

BATCH = 5000


def import_sqlite(settings: Settings, path: str) -> None:
    source = str(Path(path).resolve())
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    refs = settings.reference_apps
    with db.connect(settings, application_name="import-sqlite") as conn:
        row = conn.execute("SELECT last_id, rows_done FROM sqlite_imports WHERE source = %s",
                           (source,)).fetchone()
        last_id, done = row if row else (0, 0)
        remaining, first = src.execute(
            "SELECT count(*), min(received) FROM messages WHERE id > ?", (last_id,)).fetchone()
        if not remaining:
            log.info("%s: nothing left to import (%d rows imported earlier)", source, done)
            return
        log.info("%s: importing %d rows after id %d", source, remaining, last_id)
        # Gateway times can be a little earlier than receive times: start a day early.
        partitions.ensure_partitions(conn, int(settings.retention["partitions_ahead_days"]),
                                     since=parse_ts(first).date() - timedelta(days=1))

        started = time.monotonic()
        imported = unreadable = unstorable = 0
        while True:
            rows = src.execute(
                "SELECT id, received, message, mine FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, BATCH)).fetchall()
            if not rows:
                break
            batch = []
            for _, received, text, mine in rows:
                received_at = parse_ts(received)
                try:
                    if received_at is None:
                        raise ValueError(f"bad received time {received!r}")
                    batch.append(decode(text, received_at, refs,
                                        mine=None if mine is None else bool(mine)))
                except ValueError as exc:
                    unreadable += 1
                    log.debug("skipped unreadable row: %s", exc)
            last_id = rows[-1][0]
            with conn.transaction():
                written, bad = store(conn, batch) if batch else (0, 0)
                conn.execute(
                    """
                    INSERT INTO sqlite_imports (source, last_id, rows_done) VALUES (%s, %s, %s)
                    ON CONFLICT (source) DO UPDATE
                       SET last_id = EXCLUDED.last_id,
                           rows_done = sqlite_imports.rows_done + EXCLUDED.rows_done,
                           updated_at = now()
                    """,
                    (source, last_id, written),
                )
            imported += written
            unstorable += bad
            if imported // 100_000 != (imported - written) // 100_000:
                rate = imported / max(time.monotonic() - started, 1)
                log.info("%d / %d rows imported (%.0f/s, about %.0f min left)", imported, remaining,
                         rate, (remaining - imported) / rate / 60)
    log.info("import finished: %d imported, %d unreadable, %d unstorable, in %.0f min",
             imported, unreadable, unstorable, (time.monotonic() - started) / 60)
