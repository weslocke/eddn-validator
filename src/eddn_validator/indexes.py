"""Indexes added after launch, built with only brief, bounded write pauses.

`messages` is partitioned and always being written to. Anything that needs the whole
table to itself (a parent-level index) never gets a moment while imports hold
long per-file transactions, and queues every insert behind it meanwhile. A
CONCURRENTLY build instead waits for every open transaction before each phase, which
means hours. So these indexes live on each partition only: the planner uses a
partition's own indexes when it scans that partition, so queries on `messages` benefit
just the same. Each build blocks writes to *that one partition* for the seconds it takes,
and gives up after LOCK_TIMEOUT (retrying later) instead of queueing behind an import.
New partitions get the indexes when partitions.ensure_partitions creates them.

Run with `eddn-validator create-indexes`; it is idempotent and safe to re-run.
"""

from __future__ import annotations

import logging
import time

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

# (name, columns, partial predicate) on messages.
PARTITIONED = [
    # Pairing: "next non-reference messages after the watermark". Without it the scan walks
    # every reference row (most of the table) in id order to find the few that qualify.
    ("messages_other_id_idx", "(id)", "NOT is_big3"),
    # Facts job: "next live reference messages after the watermark", skipping archive rows.
    ("messages_live_ref_id_idx", "(id)", "is_big3 AND NOT is_test AND received_at <> gw_ts"),
    # Scorer: counts, behaviour (duplicates in content_hash order) and bursts for other apps'
    # versions, answered from the index alone instead of millions of message rows
    # (docs/research/db-performance-2026-10-14.md A7).
    # On hold (2026-10-14): measured gain at most ~20 s of EDDI 5.0.2's 76 s card for ~2.5 GB.
    # ("messages_other_beh_idx",
    #  "(app_id, software_version, content_hash, gw_ts)"
    #  " INCLUDE (event_type, msg_ts, branch, is_test, game_version)", "NOT is_big3"),
]

# Plain tables: retention deletes by age every hour.
PLAIN = [
    ("facts_system_last_seen_idx", "facts_system", "(last_seen)"),
    ("facts_body_last_seen_idx", "facts_body", "(last_seen)"),
    ("facts_station_last_seen_idx", "facts_station", "(last_seen)"),
    # Pairing looks systems up by name where the address cannot be confirmed otherwise.
    ("facts_system_name_idx", "facts_system", "(star_system)"),
    # Insert-only and in time order: a BRIN index serves retention's age deletes at a
    # fraction of a btree's size and insert cost (replaced pair_results_gw_ts_idx).
    ("pair_results_gw_ts_brin", "pair_results", "USING brin (gw_ts) WITH (pages_per_range = 32)"),
    # The scorer's mismatch queries: under 5% of rows have mismatches.
    ("pair_results_mismatch_idx", "pair_results",
     "(app_id, software_version, event_type, pair_kind, gw_ts DESC) WHERE mismatches IS NOT NULL"),
]


def _partitions(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        """
        SELECT c.relname FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE p.relname = 'messages' ORDER BY c.relname
        """)]


LOCK_TIMEOUT = "10s"
MAX_PASSES = 30


def _drop_invalid(conn: psycopg.Connection, names: list[str]) -> None:
    for (name,) in conn.execute(
            "SELECT c.relname FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
            " WHERE NOT x.indisvalid AND c.relname = ANY(%s) AND c.relkind = 'i'", (names,)).fetchall():
        conn.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(name)))
        log.info("dropped invalid leftover index %s", name)


def _build(conn: psycopg.Connection, statement: sql.Composable) -> bool:
    """Run one index build with a bounded lock wait. False if the table was busy."""
    try:
        with conn.transaction():
            conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            conn.execute(statement)
        return True
    except psycopg.errors.LockNotAvailable:
        return False


def create_indexes(conn: psycopg.Connection) -> None:
    """conn must be autocommit."""
    _drop_invalid(conn, [name for name, _, _ in PLAIN])
    for name, table, columns in PLAIN:
        started = time.monotonic()
        for _ in range(MAX_PASSES):
            if _build(conn, sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} " + columns).format(
                    sql.Identifier(name), sql.Identifier(table))):
                log.info("index %s ready (%.0fs)", name, time.monotonic() - started)
                break
            time.sleep(5)

    for attempt in range(MAX_PASSES):
        todo = [(part, name, columns, predicate) for part in _partitions(conn)
                for name, columns, predicate in PARTITIONED
                if not _has_index(conn, partition_index_name(part, name))]
        if not todo:
            break
        log.info("pass %d: %d partition indexes to build", attempt + 1, len(todo))
        for part, name, columns, predicate in todo:
            started = time.monotonic()
            if build_partition_index(conn, part, name, columns, predicate):
                log.info("  %s on %s (%.1fs)", name, part, time.monotonic() - started)
            else:
                log.info("  %s busy; will retry", part)
        time.sleep(5)
    left = sum(1 for part in _partitions(conn) for name, _, _ in PARTITIONED
               if not _has_index(conn, partition_index_name(part, name)))
    log.info("partition indexes complete" if not left else f"{left} partition indexes left (re-run)")


def partition_index_name(partition: str, name: str) -> str:
    return f"{partition}_{name.removeprefix('messages_')}"[:63]


def _has_index(conn: psycopg.Connection, index: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (index,)).fetchone()[0]


def build_partition_index(conn: psycopg.Connection, partition: str, name: str, columns: str,
                          predicate: str) -> bool:
    return _build(conn, sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} " + columns + " WHERE "
                                + predicate).format(sql.Identifier(partition_index_name(partition, name)),
                                                    sql.Identifier(partition)))


def index_new_partition(conn: psycopg.Connection, partition: str) -> None:
    """Called right after a partition is created (empty, so instant)."""
    for name, columns, predicate in PARTITIONED:
        build_partition_index(conn, partition, name, columns, predicate)
    partition_statistics(conn, partition)


def partition_statistics(conn: psycopg.Connection, partition: str) -> bool:
    """Planner statistics for one partition: software_version depends on app_id (the planner
    otherwise multiplies their selectivities and underestimates a big version's rows several
    times over), and both get a larger sample. Takes only a ShareUpdateExclusive lock."""
    stx = f"{partition}_appver_stx"[:63]
    return _build(conn, sql.SQL(
        "CREATE STATISTICS IF NOT EXISTS {} (dependencies, mcv) ON app_id, software_version,"
        " event_type FROM {}").format(sql.Identifier(stx), sql.Identifier(partition))) and _build(
        conn, sql.SQL("ALTER TABLE {} ALTER COLUMN software_version SET STATISTICS 400,"
                      " ALTER COLUMN app_id SET STATISTICS 400").format(sql.Identifier(partition)))


def analyze_partitions(conn: psycopg.Connection, only: list[str] | None = None) -> None:
    """Statistics objects on every partition (or `only`), then ANALYZE each, one at a time."""
    for part in only or _partitions(conn):
        started = time.monotonic()
        if not partition_statistics(conn, part):
            log.info("  %s busy; skipped", part)
            continue
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(part)))
        log.info("  analyzed %s (%.1fs)", part, time.monotonic() - started)


def analyze_yesterday(conn: psycopg.Connection, days_back: int = 3) -> None:
    """Daily: the last few closed partitions whose latest ANALYZE happened while they were still
    filling. Checks each against its own end, so a run that drifts past midnight misses none."""
    from datetime import datetime, time as dtime, timedelta, timezone

    from .partitions import partition_name
    today = datetime.now(timezone.utc).date()
    for back in range(1, days_back + 1):
        day = today - timedelta(days=back)
        name = partition_name(day)
        end = datetime.combine(day + timedelta(days=1), dtime.min, timezone.utc)
        row = conn.execute(
            "SELECT greatest(last_analyze, last_autoanalyze) FROM pg_stat_user_tables WHERE relname = %s",
            (name,)).fetchone()
        if row is not None and (row[0] is None or row[0] < end):
            analyze_partitions(conn, [name])
