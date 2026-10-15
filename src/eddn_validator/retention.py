"""Hourly retention (docs/DESIGN.md §5.2, config/retention.yaml).

Every message is kept for max_age_days; expired daily partitions are dropped.
A size guard removes the reference apps' oldest messages when the database drive runs
low on free space (min_free_gb), oldest day first, compacting each emptied-out day so
the space really comes back. Other apps' messages are never touched by the guard.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time

import psycopg
from psycopg import sql

from . import partitions

log = logging.getLogger(__name__)

GIB = 2**30


def database_size(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]


def import_running(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT max(value) FROM job_state"
        " WHERE name IN ('archive_import_heartbeat', 'archive_reference_heartbeat')").fetchone()
    return bool(row and row[0] and time.time() - row[0] < 3 * 3600)


GUARD_KEEP_DAYS = 7


def disk_free_gib(path: str) -> float | None:
    """Free space on the filesystem holding `path` (the database drive, mounted read-only
    into the worker), or None if the path isn't there."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize / GIB


def _compact(conn: psycopg.Connection, name: str) -> bool:
    """VACUUM FULL one day partition, waiting at most 30 s for its lock: queued behind a long
    query, the exclusive lock request would block every new statement on messages, the
    listener's included. False if the partition was busy."""
    conn.execute("SET lock_timeout = '30s'")
    try:
        conn.execute(sql.SQL("VACUUM FULL {}").format(sql.Identifier(name)))
        return True
    except psycopg.errors.LockNotAvailable:
        return False
    finally:
        conn.execute("RESET lock_timeout")


def _awaiting_compaction(conn: psycopg.Connection, name: str) -> bool:
    """A trimmed day whose space has not been returned yet (more dead rows than live ones)."""
    row = conn.execute("SELECT n_dead_tup, n_live_tup FROM pg_stat_user_tables WHERE relname = %s",
                       (name,)).fetchone()
    return bool(row and row[0] > 10_000 and row[0] > row[1])


def size_guard(conn: psycopg.Connection, cfg: dict) -> list[str]:
    """Trim reference-app messages from the oldest days while the database drive has less
    than min_free_gb free (or, if size_cap_gb is set, while the database is bigger than that).
    Other apps' messages and the newest GUARD_KEEP_DAYS days are never touched.

    A trimmed day only gives its space back once compacted, so the guard never trims another
    day while one is still waiting: a compaction that cannot get its lock stops the run, and
    the next run compacts the waiting day first. While an archive import runs the guard waits,
    unless free space falls below hard_min_free_gb."""
    min_free = float(cfg.get("min_free_gb", 60))
    hard_min = float(cfg.get("hard_min_free_gb", 52))
    probe = str(cfg.get("disk_probe", "/disk-probe"))
    cap = cfg.get("size_cap_gb")

    def reason() -> str | None:
        free = disk_free_gib(probe)
        if free is not None and free < min_free:
            return f"only {free:.0f} GB free on the database drive (minimum {min_free:.0f} GB)"
        if cap and database_size(conn) > float(cap) * GIB:
            return f"database over {cap} GB"
        return None

    trimmed: list[str] = []
    free = disk_free_gib(probe)
    if free is None:
        log.warning("size guard: %s not mounted, so free disk space is not being watched", probe)
    why = reason()
    if why is None:
        return trimmed
    if import_running(conn) and (free is None or free >= hard_min):
        log.warning("size guard: %s, but an archive import is running; waiting for it"
                    " (it acts anyway below %.0f GB free)", why, hard_min)
        return trimmed
    # The newest days stay: today's partition is being written to (VACUUM FULL would lock
    # the listener out), and the last week is what the reference apps' cards and the
    # behaviour baselines read.
    keep_from = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=GUARD_KEEP_DAYS)
    old = [name for name, day in sorted(partitions.existing_partitions(conn).items(),
                                        key=lambda kv: kv[1]) if day < keep_from]
    for name in old:
        if _awaiting_compaction(conn, name):
            if not _compact(conn, name):
                log.warning("size guard: %s is still waiting to be compacted and is busy;"
                            " trying again next run", name)
                return trimmed
            log.warning("size guard: compacted %s, trimmed earlier", name)
    for name in old:
        why = reason()
        if why is None:
            break
        with conn.transaction():
            n = conn.execute(sql.SQL("DELETE FROM {} WHERE is_big3").format(
                sql.Identifier(name))).rowcount
        if not n:
            continue
        trimmed.append(name)
        log.warning("size guard (%s): removed %d reference-app messages from %s", why, n, name)
        if not _compact(conn, name):
            # Deleting more would free nothing until this day is compacted.
            log.warning("size guard: %s is busy; compacting it (and trimming further) waits for"
                        " the next run", name)
            break
    else:
        why = reason()
        if why:
            log.error("size guard: %s, with only the last %d days of reference data left;"
                      " not trimming further", why, GUARD_KEEP_DAYS)
    return trimmed


def run(conn: psycopg.Connection, cfg: dict) -> None:
    max_age = int(cfg["max_age_days"])
    partitions.drop_expired(conn, max_age)
    trimmed = size_guard(conn, cfg)

    with conn.transaction():
        n_stats = conn.execute(
            "DELETE FROM ingest_stats WHERE minute < now() - make_interval(days => %s)",
            (max_age,),
        ).rowcount
    with conn.transaction():
        n_facts = sum(conn.execute(q, (max_age,)).rowcount for q in (
            # Pinned facts (another app's report was paired against them) are kept for good.
            "DELETE FROM facts_system WHERE NOT pinned AND last_seen < now() - make_interval(days => %s)",
            "DELETE FROM facts_body WHERE NOT pinned AND last_seen < now() - make_interval(days => %s)",
            "DELETE FROM facts_station WHERE NOT pinned AND last_seen < now() - make_interval(days => %s)",
            "DELETE FROM pair_results WHERE gw_ts < now() - make_interval(days => %s)",
        ))
        n_markets = conn.execute(
            "DELETE FROM facts_market WHERE observed_at < now() - interval '2 hours'").rowcount
    free = disk_free_gib(str(cfg.get("disk_probe", "/disk-probe")))
    log.info("retention: database %.1f GB, %s GB free on its drive; %d days trimmed by the size"
             " guard; %d ingest_stats rows, %d old facts/pairs, %d market snapshots removed",
             database_size(conn) / GIB, "?" if free is None else f"{free:.0f}", len(trimmed),
             n_stats, n_facts, n_markets)
