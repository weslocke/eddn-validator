"""Apply migrations/NNNN_*.sql in order, each in its own transaction."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)


def migrate(conn: psycopg.Connection, migrations_dir: Path) -> list[str]:
    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.stem in applied:
            continue
        log.info("applying migration %s", path.name)
        with conn.transaction():
            # A plain SET in an earlier migration would otherwise carry over into this one.
            conn.execute("RESET lock_timeout; RESET statement_timeout")
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        done.append(path.stem)
    if not done:
        log.info("database schema is up to date")
    return done
