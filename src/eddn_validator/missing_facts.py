"""Targeted pull of older reference facts (docs/DESIGN.md §24).

For an archive date range, read the reference apps' journal messages and store facts only
for the places other apps have reported that no reference app has (facts_system, facts_body,
facts_station rows missing). Nothing else is stored: no messages, no facts for places
already known.

- Systems and bodies never change, so their last_seen is the import time: retention keeps
  facts for six months after last_seen, and these would otherwise be deleted at once.
- Stations: only which system each is in (system_seen keeps the message's time); no name
  or type, which can change and are only compared against recent sightings.

Resumable: each finished file is recorded in archive_imports under a "missing-facts:" prefix.
Then the same job re-pairs all history (new facts make new pairs), rebuilds the other apps'
cards and measures the gap again (followups.measure_gap) for comparison.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone

import psycopg

from . import db, facts, followups, pairing
from .classify import game_branch, parse_ts
from .config import Settings
from .import_archive import NAME_RE, _software_name, list_files, stream_lines

log = logging.getLogger(__name__)

CATEGORIES = {"Journal.Scan", "Journal.FSDJump", "Journal.Location", "Journal.CarrierJump",
              "Journal.NavRoute", "Journal.Docked", "Journal.ApproachSettlement"}
PREFIX = "missing-facts:"
FLUSH = 20_000

TARGETS_SQL = """
SELECT DISTINCT kind, k1, k2 FROM (
    SELECT 's' AS kind, (payload->'message'->>'SystemAddress')::numeric AS k1, NULL::numeric AS k2
      FROM messages
     WHERE NOT is_big3 AND NOT is_test AND jsonb_typeof(payload->'message'->'SystemAddress') = 'number'
    UNION ALL
    -- route hops are system claims too (facts.systems_in, pairing.compare_navroute)
    SELECT 's', (hop->>'SystemAddress')::numeric, NULL
      FROM messages, jsonb_array_elements(CASE WHEN jsonb_typeof(payload->'message'->'Route') = 'array'
                                               THEN payload->'message'->'Route' ELSE '[]' END) hop
     WHERE NOT is_big3 AND NOT is_test AND event_type IN ('navroute/1', 'journal/1:NavRoute')
       AND jsonb_typeof(hop->'SystemAddress') = 'number'
    UNION ALL
    SELECT 'b', (payload->'message'->>'SystemAddress')::numeric, (payload->'message'->>'BodyID')::numeric
      FROM messages
     WHERE NOT is_big3 AND NOT is_test AND event_type = 'journal/1:Scan'
       AND jsonb_typeof(payload->'message'->'SystemAddress') = 'number'
       AND jsonb_typeof(payload->'message'->'BodyID') = 'number'
    UNION ALL
    SELECT 'm', coalesce(payload->'message'->>'MarketID', payload->'message'->>'marketId')::numeric, NULL
      FROM messages
     WHERE NOT is_big3 AND NOT is_test
       AND coalesce(jsonb_typeof(payload->'message'->'MarketID'),
                    jsonb_typeof(payload->'message'->'marketId')) = 'number'
) x
"""


def reported_and_known(conn: psycopg.Connection) -> tuple[tuple[set, set, set], tuple[set, set, set]]:
    """((systems, bodies, markets) other apps report, (those with a reference fact))."""
    with conn.transaction():
        conn.execute("SET LOCAL work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        rows = conn.execute(TARGETS_SQL).fetchall()
    limit = 2**63 - 1
    systems = {int(a) for k, a, _ in rows if k == "s" and 0 <= a < limit}
    bodies = {(int(a), int(b)) for k, a, b in rows if k == "b" and 0 <= a < limit and 0 <= b < 2**31}
    markets = {int(a) for k, a, _ in rows if k == "m" and 0 <= a < limit}
    known_s = {r[0] for r in facts.lookup(conn, "SELECT system_address FROM facts_system"
                                                " WHERE system_address = ANY(%s)", systems)}
    known_b = {(r[0], r[1]) for r in facts.lookup_bodies(conn, "system_address, body_id", bodies)}
    known_m = {r[0] for r in facts.lookup(conn, "SELECT market_id FROM facts_station"
                                                " WHERE market_id = ANY(%s)", markets)}
    return (systems, bodies, markets), (known_s, known_b, known_m)


def missing_targets(conn: psycopg.Connection) -> tuple[set, set, set]:
    """(system addresses, (system, body) keys, market ids) other apps report and no reference
    fact covers."""
    (systems, bodies, markets), (known_s, known_b, known_m) = reported_and_known(conn)
    return systems - known_s, bodies - known_b, {m for m in markets - known_m
                                                if facts.persistent_market(m)}


def pin_reported(settings: Settings) -> None:
    """Pin every fact that other apps' messages on file report (kept for good by retention)."""
    started = time.monotonic()
    with db.connect(settings, application_name="missing-facts") as conn:
        _, known = reported_and_known(conn)
        facts.pin_in_slices(conn, *known)
    log.info("pinned the facts other apps report: %d systems, %d bodies, %d stations (%.0f min)",
             *map(len, known), (time.monotonic() - started) / 60)


_settings: Settings | None = None
_targets: tuple[set, set, set] = (set(), set(), set())
_lock = None


def _init(settings: Settings, targets: tuple[set, set, set], lock) -> None:
    global _settings, _targets, _lock
    _settings, _targets, _lock = settings, targets, lock
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _flush(conn: psycopg.Connection, batch: facts.Batch, now: datetime) -> int:
    systems, bodies, markets = _targets
    keep = facts.Batch()
    keep.systems = {k: {**v, "last": now} for k, v in batch.systems.items() if k in systems}
    keep.bodies = {k: {**v, "last": now} for k, v in batch.bodies.items() if k in bodies}
    keep.stations = {k: {**v, "name": None, "type": None, "last": now}
                     for k, v in batch.stations.items() if k in markets and v.get("system")}
    n = len(keep.systems) + len(keep.bodies) + len(keep.stations)
    if n:
        with _lock, conn.transaction():
            facts.store(conn, keep)
    return n


def import_file(job: tuple[str, str, date]) -> tuple[str, int, int, int, str | None]:
    """Returns (url, lines, reference messages, fact rows stored, error)."""
    url, category, _ = job
    refs = _settings.reference_apps
    error = None
    for attempt in range(3):
        lines = ref_n = stored = 0
        try:
            now = datetime.now(timezone.utc)
            with db.connect(_settings, application_name="missing-facts") as conn:
                batch, pending = facts.Batch(), 0
                for raw in stream_lines(url):
                    if not raw.strip():
                        continue
                    lines += 1
                    text = raw.decode("utf-8", "replace")
                    group = refs.group_for(_software_name(NAME_RE.search(text)))
                    if group is None:
                        continue
                    try:
                        envelope = json.loads(text)
                        header = envelope.get("header") or {}
                        message = envelope.get("message")
                        gw = parse_ts(header.get("gatewayTimestamp"))
                        if not (isinstance(message, dict) and gw):
                            continue
                        branch = game_branch(header.get("gameversion"))
                        if branch == "unknown" and message.get("odyssey") is True:
                            # Before Update 14 (Nov 2022) messages had no gameversion header;
                            # an Odyssey client marks its data with odyssey=true.
                            branch = "live"
                        if branch != "live":
                            continue    # live game only, systems included (DESIGN §24)
                        event = message.get("event")
                        event_type = f"journal/1:{event}" if isinstance(event, str) else "?"
                        if category == "Journal.ApproachSettlement":
                            event_type = "approachsettlement/1"
                        batch.add(event_type, message, branch, "journal", group, gw, None)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    ref_n += 1
                    pending += 1
                    if pending >= FLUSH:
                        stored += _flush(conn, batch, now)
                        batch, pending = facts.Batch(), 0
                stored += _flush(conn, batch, now)
                conn.execute(
                    "INSERT INTO archive_imports (url, lines, stored, folded) VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (url) DO UPDATE SET lines = EXCLUDED.lines, stored = EXCLUDED.stored,"
                    " folded = EXCLUDED.folded, finished_at = now()", (PREFIX + url, lines, stored, ref_n))
            return url, lines, ref_n, stored, None
        except Exception as exc:   # network trouble, a cut-off download: retry the whole file
            log.warning("%s attempt %d failed: %s", url.rsplit("/", 1)[-1], attempt + 1, exc)
            time.sleep(10 * (attempt + 1))
            error = str(exc)
    return url, lines, ref_n, stored, error


def import_missing(settings: Settings, since: date, until: date, workers: int = 6) -> int:
    started = time.monotonic()
    with db.connect(settings, application_name="missing-facts") as conn:
        targets = missing_targets(conn)
        done = {r[0][len(PREFIX):] for r in conn.execute(
            "SELECT url FROM archive_imports WHERE url LIKE %s", (PREFIX + "%",))}
    log.info("targets: %d systems, %d bodies, %d stations with no reference fact (%.0f min)",
             *map(len, targets), (time.monotonic() - started) / 60)
    files = [f for f in list_files(since, until, include_test=False)
             if f[1] in CATEGORIES and f[0] not in done]
    log.info("%d archive files to read (%s to %s), %d already done", len(files), since, until, len(done))
    totals, failed = [0, 0, 0], []
    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init,
                             initargs=(settings, targets, ctx.Lock())) as pool:
        futures = [pool.submit(import_file, f) for f in files]
        for i, future in enumerate(as_completed(futures), 1):
            url, lines, ref_n, stored, error = future.result()
            if error:
                failed.append(url.rsplit("/", 1)[-1])
                log.error("%s failed: %s", url.rsplit("/", 1)[-1], error)
                continue
            totals[0] += lines
            totals[1] += ref_n
            totals[2] += stored
            elapsed = time.monotonic() - started
            log.info("[%d/%d] %s: %d reference messages, %d fact rows stored (%.0f min)",
                     i, len(files), url.rsplit("/", 1)[-1], ref_n, stored, elapsed / 60)
    log.info("missing facts: %d lines, %d reference messages read, %d fact rows stored, %d files failed",
             *totals, len(failed))
    return len(failed)


def run(settings: Settings, since: date, until: date, workers: int = 6) -> None:
    """The pull, then a full re-pair, the other apps' cards rebuilt, and the gap measured again."""
    if import_missing(settings, since, until, workers):
        raise SystemExit("some files failed; re-run to retry them (finished files are skipped)")
    with db.connect(settings, application_name="missing-facts") as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (pairing.PAIRING_LOCK,))
        try:
            conn.execute("INSERT INTO job_state (name, value) VALUES ('pairing_last_id', 0)"
                         " ON CONFLICT (name) DO UPDATE SET value = 0, updated_at = now()")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (pairing.PAIRING_LOCK,))
    log.info("re-pairing all history against the new facts")
    followups._step("re-pair", _repair_until_done, settings)
    followups._step("pin reported facts", pin_reported, settings)
    followups._step("rebuild other apps' scorecards", followups.rebuild_other_cards, settings)
    followups._step("pairing-gap measurement", followups.measure_gap, settings)
    log.info("missing facts: complete")


def _repair_until_done(settings: Settings) -> None:
    """One pairing run over everything after the watermark, then a check that it really got
    there: pairing pauses (and returns quietly) while an archive import runs or while facts
    catch up, and the measurement must not run on half-paired history."""
    with db.connect(settings, application_name="pair-backlog") as conn:
        pairing.run(conn, max_seconds=48 * 3600, wait=True)
    with db.connect(settings, application_name="missing-facts") as conn:
        left = conn.execute(
            "SELECT count(*) FROM messages WHERE NOT is_big3 AND received_at < now() - interval '1 hour'"
            " AND id > (SELECT value FROM job_state WHERE name = 'pairing_last_id')").fetchone()[0]
    if left:
        raise RuntimeError(f"re-pair incomplete: {left} messages still to pair (an import running?)")
