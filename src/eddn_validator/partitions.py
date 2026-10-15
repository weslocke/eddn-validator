"""Daily partitions of the messages table."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^messages_p(\d{8})$")


def partition_name(day: date) -> str:
    return f"messages_p{day:%Y%m%d}"


def days_to_cover(today: date, days_ahead: int, since: date | None = None) -> list[date]:
    """Every day from `since` (default today) through today + days_ahead."""
    first = min(since or today, today)
    return [first + timedelta(days=n) for n in range((today - first).days + days_ahead + 1)]


def ensure_partitions(conn: psycopg.Connection, days_ahead: int,
                      since: date | None = None) -> int:
    """Create daily partitions from `since` (default today, UTC) through today + days_ahead."""
    created = 0
    for day in days_to_cover(datetime.now(timezone.utc).date(), days_ahead, since):
        name = partition_name(day)
        exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]
        if exists:
            continue
        lower = datetime.combine(day, datetime.min.time(), timezone.utc)
        upper = lower + timedelta(days=1)
        try:
            with conn.transaction():
                # Attaching needs an exclusive lock on messages; don't queue behind a long
                # import transaction (and block every insert meanwhile). Partitions are made
                # days ahead, so a skipped attempt is simply retried next run.
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} PARTITION OF messages FOR VALUES FROM ({}) TO ({})"
                ).format(sql.Identifier(name), sql.Literal(lower), sql.Literal(upper)))
        except psycopg.errors.LockNotAvailable:
            log.warning("partition %s not created: messages is busy; will retry", name)
            continue
        except psycopg.errors.CheckViolation:
            # Rows for this day already sit in messages_default (no partition existed when
            # they arrived). Keep going with the other days; moving the rows out is a manual
            # repair (docs/DESIGN.md §22), never done behind the listener's back.
            log.error("partition %s cannot be created: messages_default holds rows for that day;"
                      " see docs/DESIGN.md §22 to move them", name)
            continue
        from .indexes import index_new_partition
        index_new_partition(conn, name)
        created += 1
        log.info("created partition %s", name)
    return created


def existing_partitions(conn: psycopg.Connection) -> dict[str, date]:
    rows = conn.execute(
        """
        SELECT c.relname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = 'messages'
        """
    ).fetchall()
    found = {}
    for (name,) in rows:
        m = NAME_RE.match(name)
        if m:
            found[name] = datetime.strptime(m.group(1), "%Y%m%d").date()
    return found


def drop_expired(conn: psycopg.Connection, max_age_days: int) -> list[str]:
    """Drop daily partitions whose whole day is older than max_age_days."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)
    dropped = []
    for name, day in sorted(existing_partitions(conn).items(), key=lambda kv: kv[1]):
        if day >= cutoff:
            continue
        try:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
        except psycopg.errors.LockNotAvailable:
            log.warning("partition %s not dropped: messages is busy; will retry", name)
            continue
        dropped.append(name)
        log.info("dropped expired partition %s", name)
    # Rows that ended up in the default partition are never dropped with a day: expire them
    # here, and say so whenever the default partition is in use at all.
    with conn.transaction():
        n = conn.execute("DELETE FROM messages_default WHERE gw_ts < %s",
                         (datetime.combine(cutoff, datetime.min.time(), timezone.utc),)).rowcount
    if n:
        log.info("removed %d expired rows from messages_default", n)
    if conn.execute("SELECT EXISTS (SELECT 1 FROM messages_default)").fetchone()[0]:
        log.warning("messages_default holds rows: a day had no partition when they arrived"
                    " (see docs/DESIGN.md §22)")
    return dropped
