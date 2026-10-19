"""Unattended follow-up work after the 2026-10 history backfill, run once in its own container.

1. Wait for the running archive import and pair-backlog to finish (their database sessions
   are gone).
2. Fill station systems, mark every scorecard due, and report the database size.
2b. Re-pair all history so station results include the station's system (repair_stations).
3. For each day still to import (2026-10-13 and -14): wait until edgalaxydata.space has
   published that day's files, then run the gap-filling pass and the reference pass.
4. Pair the newly imported days, mark scorecards due again.
5. Measure the pairing gap: of other apps' reports that name a system, body or station, how
   many have no reference-app fact to compare with. That decides whether a further six months
   of reference facts is worth importing; nothing is imported here.

Progress and the final report go to the container's log: `docker logs eddn-validator-followups`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import psycopg

from . import db, facts, pairing
from .config import Settings
from .id64 import is_procedural
from .import_archive import import_archive, list_files

log = logging.getLogger(__name__)

DAYS = (date(2026, 10, 13), date(2026, 10, 14))
POLL = 300                 # seconds between checks while waiting
CHUNK = 20000


def _connect(settings: Settings) -> psycopg.Connection:
    return db.connect(settings, application_name="followups")


def _busy(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT application_name FROM pg_stat_activity"
        " WHERE application_name LIKE 'eddn-validator import-archive%'"
        " OR application_name = 'eddn-validator pair-backlog'")]


def wait_for_jobs(settings: Settings) -> None:
    while True:
        with _connect(settings) as conn:
            busy = _busy(conn)
        if not busy:
            log.info("no archive import or pair-backlog running")
            return
        log.info("waiting for %s to finish", ", ".join(sorted(busy)))
        time.sleep(POLL)


def after_backfill(settings: Settings) -> None:
    with _connect(settings) as conn:
        n = facts.backfill_station_systems(conn)
        log.info("station systems filled: %d stations updated", n)
        _rescore(conn)
        _report_size(conn)


def _rescore(conn: psycopg.Connection) -> None:
    n = conn.execute("UPDATE scorecards SET computed_at = computed_at - interval '7 hours'").rowcount
    log.info("%d scorecards marked due; the worker rebuilds them over its next runs", n)


def _report_size(conn: psycopg.Connection) -> None:
    gb = conn.execute("SELECT pg_database_size(current_database()) / 1e9").fetchone()[0]
    log.info("database size %.1f GB (the owner's rule: keep 50 GB free on its drive)", gb)


def _published(day: date) -> int:
    """How many of the day's files are listed, once the listing has stopped growing."""
    first = len(list_files(day, day, include_test=True))
    if not first:
        return 0
    time.sleep(1200)        # files appear one by one: wait until the count holds still
    second = len(list_files(day, day, include_test=True))
    return second if second == first else 0


SHORT_DAY_WAIT = 6 * 3600   # a day listing fewer files than the day before is accepted after this


def import_day(settings: Settings, day: date) -> None:
    """Wait until the day's archive files are all published, then import them (both passes).
    The reference pass deletes the whole day's reference rows before refilling them from the
    listed files, so a file published late would be lost: wait for as many files as the day
    before had, and accept fewer only once the listing has stayed short for SHORT_DAY_WAIT."""
    first_seen_short = None
    while True:
        try:
            expected = len(list_files(day - timedelta(days=1), day - timedelta(days=1),
                                      include_test=True))
            n = _published(day)
        except OSError as exc:
            log.warning("archive listing failed (%s); retrying", exc)
            expected = n = 0
        if n and expected and datetime.now(timezone.utc).date() > day:
            if n >= expected:
                break
            first_seen_short = first_seen_short or time.monotonic()
            if n >= 0.9 * expected and time.monotonic() - first_seen_short > SHORT_DAY_WAIT:
                log.warning("%s: only %d of about %d files after %d hours; importing what is there",
                            day, n, expected, SHORT_DAY_WAIT // 3600)
                break
        log.info("waiting for %s archive files (%d listed, about %d expected)", day, n, expected)
        time.sleep(POLL)
    log.info("importing %s: gap-filling pass", day)
    if import_archive(settings, since=day, until=day, workers=8):
        raise RuntimeError(f"{day}: some files failed in the gap-filling pass")
    log.info("importing %s: reference pass", day)
    if import_archive(settings, since=day, until=day, workers=8, reference=True):
        raise RuntimeError(f"{day}: some files failed in the reference pass")


REPAIR_STATE = "station_repair"     # job_state: 1 = old station results cleared, 2 = done


def repair_stations(settings: Settings) -> None:
    """Re-pair all history once station systems are filled in. The backlog was paired before
    facts_station knew each station's system, so station results compared only name and
    type. Only station results are cleared: market results can't be redone (market snapshots
    are kept for 2 hours) and the others are re-checked in passing (existing rows are kept,
    pairs whose reference fact has appeared since are added). Resumable: a retry continues
    from the pairing watermark instead of starting over."""
    with _connect(settings) as conn:
        state = conn.execute("SELECT value FROM job_state WHERE name = %s",
                             (REPAIR_STATE,)).fetchone()
        if state and state[0] >= 2:
            log.info("station re-pair already done")
            return
        if not state:
            # Hold the pairing lock while clearing, so a worker run can't pair in between.
            conn.execute("SELECT pg_advisory_lock(%s)", (pairing.PAIRING_LOCK,))
            try:
                with conn.transaction():
                    n = conn.execute("DELETE FROM pair_results WHERE pair_kind = 'station'").rowcount
                    conn.execute("INSERT INTO job_state (name, value) VALUES ('pairing_last_id', 0)"
                                 " ON CONFLICT (name) DO UPDATE SET value = 0, updated_at = now()")
                    conn.execute("INSERT INTO job_state (name, value) VALUES (%s, 1)", (REPAIR_STATE,))
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (pairing.PAIRING_LOCK,))
            log.info("station re-pair: cleared %d station results; pairing all history again", n)
    with db.connect(settings, application_name="pair-backlog") as conn:
        pairing.run(conn, max_seconds=48 * 3600, wait=True)
    with _connect(settings) as conn:
        left = conn.execute(
            "SELECT count(*) FROM messages WHERE NOT is_big3 AND received_at < now() - interval '1 hour'"
            " AND id > (SELECT value FROM job_state WHERE name = 'pairing_last_id')").fetchone()[0]
        if left:    # pairing stopped early (facts catching up, say): the step's retry continues it
            raise RuntimeError(f"station re-pair incomplete: {left} messages still to pair")
        conn.execute("UPDATE job_state SET value = 2, updated_at = now() WHERE name = %s",
                     (REPAIR_STATE,))
        log.info("station re-pair finished")
        _rescore(conn)


REBUILD_THREADS = 3


def rebuild_other_cards(settings: Settings, limit: int | None = None) -> None:
    """Rebuild every non-reference app version's card now, smallest versions first (the
    heavy hitters with millions of messages go last, so most cards refresh quickly), so
    the re-paired results show up within the hour instead of over the worker's 10-minute
    slices. Reference apps aren't paired, so their cards are left to the worker."""
    from concurrent.futures import ThreadPoolExecutor

    from .scorer import ALL_VERSIONS, Scorer

    with _connect(settings) as conn:
        todo = conn.execute(
            """
            SELECT a.id, a.software_name, a.ref_group, v.version
              FROM app_versions v JOIN apps a ON a.id = v.app_id
             WHERE a.ref_group IS NULL AND v.last_seen > now() - interval '183 days'
             ORDER BY v.msg_count, v.last_seen DESC
             LIMIT %s
            """, (limit,)).fetchall()
        # Then the all-versions cards (scorer.ALL_VERSIONS), smallest app first: each covers
        # every version on file, so they are the biggest and come last.
        todo += conn.execute(
            """
            SELECT a.id, a.software_name, a.ref_group, %s
              FROM apps a JOIN app_versions v ON v.app_id = a.id
             WHERE a.ref_group IS NULL AND v.last_seen > now() - interval '180 days'
             GROUP BY a.id, a.software_name, a.ref_group
            HAVING count(*) >= 2
             ORDER BY sum(v.msg_count)
            """, (ALL_VERSIONS,)).fetchall()
    log.info("rebuilding %d other-app scorecards, %d at a time", len(todo), REBUILD_THREADS)
    started = time.monotonic()
    # One shared queue, so the threads take versions strictly in size order.
    queue = list(reversed(todo))
    lock = threading.Lock()

    def take():
        with lock:
            return queue.pop() if queue else None

    def work(_: int) -> int:
        done = 0
        with db.connect(settings, application_name="followups rebuild") as conn:
            scorer = Scorer(conn, settings)
            while (item := take()) is not None:
                app_id, name, group, version = item
                try:
                    scorer.score_version({"id": app_id, "name": name, "ref_group": group}, version)
                except Exception:
                    log.exception("rebuilding %s %s failed", name, version)
                done += 1
                if done % 100 == 0:
                    log.info("rebuild: %d done in this thread, %d left in all (%.0f min)", done,
                             len(queue), (time.monotonic() - started) / 60)
        return done

    with ThreadPoolExecutor(REBUILD_THREADS) as pool:
        total = sum(pool.map(work, range(REBUILD_THREADS)))
    log.info("rebuilt %d other-app scorecards in %.0f min", total, (time.monotonic() - started) / 60)


def pair_and_rescore(settings: Settings) -> None:
    with db.connect(settings, application_name="pair-backlog") as conn:
        pairing.run(conn, max_seconds=12 * 3600, wait=True)
    with _connect(settings) as conn:
        _rescore(conn)
        _report_size(conn)


def measure_gap(settings: Settings) -> None:
    """For other apps' messages on file, the share of system, body and station claims with no
    reference-app fact, i.e. claims pairing cannot check."""
    claims: Counter = Counter()
    missing: Counter = Counter()
    missing_named = 0
    distinct_missing: dict[str, set] = {"system": set(), "body": set(), "station": set()}
    last, seen, started = 0, 0, time.monotonic()
    with _connect(settings) as conn:
        top = conn.execute("SELECT max(id) FROM messages WHERE NOT is_big3").fetchone()[0] or 0
        while True:
            rows = conn.execute(
                "SELECT id, event_type, payload->'message' FROM messages"
                " WHERE id > %s AND NOT is_big3 AND NOT is_test ORDER BY id LIMIT %s",
                (last, CHUNK)).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            systems, bodies, stations = {}, {}, {}
            for _, event_type, m in rows:
                if not isinstance(m, dict):
                    continue
                try:    # sender-controlled JSON: one odd message must not stop the measurement
                    for address, name, _ in facts.systems_in(m):
                        systems.setdefault(facts.db_int(address), []).append(name)
                    if event_type == "journal/1:Scan":
                        key = (facts.db_int(m.get("SystemAddress")),
                               facts.db_int(m.get("BodyID"), facts.INT32_MAX))
                        if None not in key:
                            bodies[key] = bodies.get(key, 0) + 1
                    station = facts.station_in(event_type, m)
                except Exception:
                    continue
                if station:
                    stations[station[0]] = stations.get(station[0], 0) + 1
            known_s = {r[0] for r in facts.lookup(
                conn, "SELECT system_address FROM facts_system WHERE NOT conflict"
                      " AND system_address = ANY(%s)", systems)}
            known_b = {(r[0], r[1]) for r in facts.lookup_bodies(conn, "system_address, body_id", bodies)}
            known_m = {r[0] for r in facts.lookup(
                conn, "SELECT market_id FROM facts_station WHERE market_id = ANY(%s)", stations)}
            for address, names in systems.items():
                claims["system"] += len(names)
                if address not in known_s:
                    missing["system"] += len(names)
                    missing_named += sum(1 for n in names if not is_procedural(n))
                    distinct_missing["system"].add(address)
            for key, n in bodies.items():
                claims["body"] += n
                if key not in known_b:
                    missing["body"] += n
                    distinct_missing["body"].add(key)
            for market, n in stations.items():
                claims["station"] += n
                if market not in known_m:
                    missing["station"] += n
                    distinct_missing["station"].add(market)
            seen += len(rows)
            if seen % (CHUNK * 50) == 0:
                log.info("gap measurement: %d messages read (id %d of %d, %.0f min)",
                         seen, last, top, (time.monotonic() - started) / 60)
    log.info("PAIRING GAP REPORT (other apps' messages on file: %d read)", seen)
    for kind in ("system", "body", "station"):
        total, gap = claims[kind], missing[kind]
        log.info("  %-7s %11d claims, %11d with no reference fact (%.1f%%), %d distinct places",
                 kind, total, gap, 100 * gap / total if total else 0.0, len(distinct_missing[kind]))
    if missing["system"]:
        log.info("  of the system claims with no fact, %d (%.1f%%) are hand-named systems, which the"
                 " system-ID checks cannot cover", missing_named, 100 * missing_named / missing["system"])
    log.info("Decision for the owner: a further six months of reference facts would close part of"
             " these gaps (places visited by a reference app 6-12 months ago). Nothing was imported.")


RETRIES = 24        # per step, POLL apart: two hours of outages before a step gives up


def _step(name: str, fn, *args) -> None:
    """Run one step, retrying on any error: nobody is watching to restart the job. Every
    step is safe to repeat (imports skip finished files, the rest is idempotent)."""
    for attempt in range(1, RETRIES + 1):
        try:
            fn(*args)
            return
        except Exception:
            log.exception("%s failed (attempt %d of %d); retrying in %d s", name, attempt, RETRIES, POLL)
            time.sleep(POLL)
    log.error("%s gave up after %d attempts; continuing with the next step", name, RETRIES)


STEPS = ("wait", "backfill", "import-13", "repair", "rebuild", "measure", "import-14", "final")


def run(settings: Settings, start: str = "wait") -> None:
    """Run the steps in order, from `start` (one of STEPS) onwards. Order chosen 2026-10-14:
    the re-pair runs after the 13th is imported (so that day is re-paired in the same pass),
    and neither it nor the measurement waits for the 14th's files (published about 01:30 UTC
    on the 16th)."""
    steps = {
        "wait": ("waiting for running jobs", wait_for_jobs, ()),
        "backfill": ("station systems and rescore", after_backfill, ()),
        "import-13": (f"import {DAYS[0]}", import_day, (DAYS[0],)),
        "repair": ("station re-pair", repair_stations, ()),
        "rebuild": ("rebuild other apps' scorecards", rebuild_other_cards, ()),
        "measure": ("pairing-gap measurement", measure_gap, ()),
        "import-14": (f"import {DAYS[1]}", import_day, (DAYS[1],)),
        "final": ("pairing and rescore", pair_and_rescore, ()),
    }
    for key in STEPS[STEPS.index(start):]:
        name, fn, args = steps[key]
        log.info("step %s: %s", key, name)
        _step(name, fn, settings, *args)
    log.info("follow-ups complete")
