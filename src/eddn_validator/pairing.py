"""Same-object pairing (layer 4, docs/DESIGN.md §6.4).

Every few minutes the worker
  1. folds new reference-app messages into the fact tables, then
  2. compares other apps' messages that are at least PAIR_DELAY old (so the
     reference side has had time to arrive) with those facts, writing one
     pair_results row per comparison.

Only static data is compared, except markets, which are compared within a
short window and count as "soft" evidence because prices can tick between
two players' snapshots.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

import psycopg

from . import facts
from .id64 import decode as decode_id64
from .id64 import name_matches_address

log = logging.getLogger(__name__)

PAIR_DELAY = timedelta(minutes=10)
MARKET_WINDOW = timedelta(minutes=5)
STATION_FRESH = timedelta(days=7)
CHUNK = 5000
MAX_SECONDS = 240   # leave the rest for the next run
MAX_MISMATCHES = 10
HEARTBEAT_STALE = 3 * 3600   # an import's marker older than this is treated as a crashed import


def _get_state(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT value FROM job_state WHERE name = %s", (name,)).fetchone()
    return row[0] if row else 0


def _set_state(conn: psycopg.Connection, name: str, value: int) -> None:
    conn.execute("INSERT INTO job_state (name, value) VALUES (%s, %s) ON CONFLICT (name)"
                 " DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (name, value))


# ------------------------------------------------------------------ facts

def update_facts(conn: psycopg.Connection, deadline: float) -> tuple[int, bool]:
    """Fold new reference messages into the facts. Returns (messages done, caught up)."""
    done = 0
    while time.monotonic() < deadline:
        last = _get_state(conn, "facts_last_id")
        rows = conn.execute(
            """
            SELECT m.id, m.event_type, m.payload->'message', m.branch, m.source, a.ref_group,
                   m.gw_ts, m.msg_ts
              FROM messages m JOIN apps a ON a.id = m.app_id
             WHERE m.id > %s AND m.is_big3 AND NOT m.is_test
               AND m.received_at <> m.gw_ts   -- archive rows: facts folded in at import
             ORDER BY m.id LIMIT %s
            """, (last, CHUNK)).fetchall()
        if not rows:
            return done, True
        batch = facts.Batch()
        for mid, event_type, message, branch, source, group, gw_ts, msg_ts in rows:
            if isinstance(message, dict):
                try:
                    batch.add(event_type, message, branch, source, group, gw_ts, msg_ts)
                except Exception:   # one odd message must never stall the facts job
                    log.exception("facts skipped message %s", mid)
        with conn.transaction():
            facts.store(conn, batch)
            _set_state(conn, "facts_last_id", rows[-1][0])
        done += len(rows)
    return done, False


# ---------------------------------------------------------------- compare

def _mismatch(out: dict, key: str, app_value: object, ref_value: object) -> None:
    if len(out) < MAX_MISMATCHES:
        out[key] = [app_value, ref_value]


def address_by_name(address: int, name: str, pos: tuple, by_name: dict) -> int | None:
    """The real SystemAddress of a system the claim's address has no fact for, found by name
    AND position (two systems can share a name, never a name and a position), when it differs
    from the claimed one. Only where no rule can see a wrong address: the name cannot confirm
    it (hand-named systems, hand-made sectors: GEN-033 returns None) and the position lies in
    the boxel it encodes (GEN-030 passes). Elsewhere those rules already report it."""
    candidates = [a for a, p in by_name.get(name, ()) if facts.same_pos(pos, tuple(p))]
    if len(candidates) != 1 or candidates[0] == address:
        return None
    if name_matches_address(name, address) is not None:
        return None
    boxel = decode_id64(address)
    if boxel is None or not boxel.contains(pos):
        return None
    return candidates[0]


def compare_system(message: dict, known: dict, by_name: dict | None = None) -> tuple[int, int, dict] | None:
    claims = facts.systems_in({k: v for k, v in message.items() if k != "Route"})
    if not claims:
        return None
    address, name, pos = claims[0]
    fact = known.get(address)
    if fact is None:
        real = address_by_name(address, name, pos, by_name or {})
        return (1, 1, {"SystemAddress": [address, real]}) if real is not None else None
    ref_name, ref_pos = fact
    out: dict = {}
    if name != ref_name:
        _mismatch(out, "StarSystem", name, ref_name)
    if not facts.same_pos(pos, tuple(ref_pos)):
        _mismatch(out, "StarPos", list(pos), list(ref_pos))
    return 2, len(out), out


def compare_navroute(message: dict, known: dict, by_name: dict | None = None) -> tuple[int, int, dict] | None:
    compared = mismatched = 0
    out: dict = {}
    for i, hop in enumerate(message.get("Route") or []):
        claims = facts.systems_in(hop) if isinstance(hop, dict) else []
        if not claims:
            continue
        address, name, pos = claims[0]
        if address not in known:
            real = address_by_name(address, name, pos, by_name or {})
            if real is not None:
                compared += 1
                mismatched += 1
                _mismatch(out, f"Route[{i}].SystemAddress", address, real)
            continue
        ref_name, ref_pos = known[address]
        compared += 2
        if name != ref_name:
            mismatched += 1
            _mismatch(out, f"Route[{i}].StarSystem", name, ref_name)
        if not facts.same_pos(pos, tuple(ref_pos)):
            mismatched += 1
            _mismatch(out, f"Route[{i}].StarPos", list(pos), list(ref_pos))
    return (compared, mismatched, out) if compared else None


def compare_body(message: dict, known: dict) -> tuple[int, int, dict] | None:
    key = (facts.db_int(message.get("SystemAddress")), facts.db_int(message.get("BodyID"), facts.INT32_MAX))
    if None in key:
        return None
    fact = known.get(key)
    if fact is None:
        return None
    ref_name, static = fact
    compared = mismatched = 0
    out: dict = {}
    if isinstance(message.get("BodyName"), str) and ref_name:
        compared += 1
        if message["BodyName"] != ref_name:
            mismatched += 1
            _mismatch(out, "BodyName", message["BodyName"], ref_name)
    for field, ref_value in static.items():
        if field in message:
            compared += 1
            if not facts.same(message[field], ref_value):
                mismatched += 1
                _mismatch(out, field, message[field], ref_value)
    return (compared, mismatched, out) if compared else None


def compare_station(event_type: str, message: dict, gw_ts, known: dict) -> tuple[int, int, dict] | None:
    station = facts.station_in(event_type, message)
    if not station or station[0] not in known:
        return None
    market, name, kind = station
    ref_name, ref_type, last_seen, ref_system = known[market]
    compared = mismatched = 0
    out: dict = {}
    # Name and type change (carrier renames, station rebuilds): compare only recent facts.
    if abs(gw_ts - last_seen) <= STATION_FRESH:
        if name and ref_name:
            compared += 1
            if name.lower() != ref_name.lower():
                mismatched += 1
                _mismatch(out, "StationName", name, ref_name)
        if kind and ref_type:
            compared += 1
            if kind != facts.station_type_key(ref_type):
                mismatched += 1
                _mismatch(out, "StationType", kind, ref_type)
    # Which system a station is in never changes, unless it is a carrier/megaship.
    system, _ = facts.station_system(event_type, message)
    if system and ref_system and not facts.moving_station(market, kind or facts.station_type_key(ref_type)):
        compared += 1
        if system != ref_system:
            mismatched += 1
            _mismatch(out, "system", system, ref_system)
    return (compared, mismatched, out) if compared else None


def compare_market(message: dict, msg_ts, source: str, snapshots: list) -> tuple[int, int, dict] | None:
    if msg_ts is None:
        return None
    candidates = [s for s in snapshots
                  if s[1] == source and abs(s[0] - msg_ts) <= MARKET_WINDOW]
    if not candidates:
        return None
    ref = min(candidates, key=lambda s: abs(s[0] - msg_ts))[2]
    mine = facts.market_snapshot(message)
    out: dict = {}
    compared, mismatched = 1, 0
    if set(mine) != set(ref):
        mismatched += 1
        _mismatch(out, "commodities", sorted(set(mine) - set(ref))[:5],
                  sorted(set(ref) - set(mine))[:5])
    for name in sorted(set(mine) & set(ref)):
        compared += 1
        if mine[name][:2] != ref[name][:2]:
            mismatched += 1
            _mismatch(out, f"{name}.buy/sell", mine[name][:2], ref[name][:2])
    return compared, mismatched, out


def compare_all(m: dict, event_type: str, gw_ts, msg_ts, source: str, known_systems: dict,
               known_bodies: dict, known_stations: dict, snapshots: dict,
               by_name: dict | None = None) -> dict:
    market = facts.db_int(m.get("marketId"))
    return {
        "system": compare_system(m, known_systems, by_name),
        "navroute": compare_navroute(m, known_systems, by_name) if "Route" in m else None,
        "body": compare_body(m, known_bodies) if event_type == "journal/1:Scan" else None,
        "station": compare_station(event_type, m, gw_ts, known_stations),
        "market": (compare_market(m, msg_ts, source, snapshots.get(market, []))
                   if event_type == "commodity/3" and market is not None else None),
    }


def pair_messages(conn: psycopg.Connection, deadline: float, upto: int | None = None) -> int:
    """Pair messages after the watermark, up to id `upto` (all eligible ones if None)."""
    done = 0
    while time.monotonic() < deadline:
        last = _get_state(conn, "pairing_last_id")
        rows = conn.execute(
            """
            SELECT id, gw_ts, msg_ts, app_id, software_version, event_type, source,
                   payload->'message', branch
              FROM messages
             WHERE id > %s AND id <= %s AND NOT is_big3 AND received_at < now() - %s
             ORDER BY id LIMIT %s
            """, (last, upto if upto is not None else 2**63 - 1, PAIR_DELAY, CHUNK)).fetchall()
        if not rows:
            break
        messages = [r for r in rows if isinstance(r[7], dict)]
        addresses, bodies, markets = set(), set(), set()
        for _, _, _, _, _, event_type, _, m, _ in messages:
            addresses.update(a for a, _, _ in facts.systems_in(m))
            if event_type == "journal/1:Scan":
                body_key = (facts.db_int(m.get("SystemAddress")),
                            facts.db_int(m.get("BodyID"), facts.INT32_MAX))
                if None not in body_key:
                    bodies.add(body_key)
            station = facts.station_in(event_type, m)
            if station:
                markets.add(station[0])
        rows_s = facts.lookup(conn, "SELECT system_address, star_system, star_pos, pinned"
                                    " FROM facts_system WHERE NOT conflict AND system_address = ANY(%s)",
                              addresses)
        rows_b = facts.lookup_bodies(conn, "system_address, body_id, body_name, static, pinned", bodies)
        rows_m = facts.lookup(conn, "SELECT market_id, station_name, station_type, last_seen,"
                                    " star_system, pinned FROM facts_station WHERE market_id = ANY(%s)",
                              markets)
        known_systems = {r[0]: (r[1], r[2]) for r in rows_s}
        # Claims whose address has no fact, where only the name can show a wrong address.
        names = {name for _, _, _, _, _, _, _, m, _ in messages for a, name, _ in facts.systems_in(m)
                 if a not in known_systems and name_matches_address(name, a) is None}
        by_name: dict[str, list] = {}
        for address, name, pos in facts.lookup(
                conn, "SELECT system_address, star_system, star_pos FROM facts_system"
                      " WHERE NOT conflict AND star_system = ANY(%s)", names):
            by_name.setdefault(name, []).append((address, pos))
        known_bodies = {(r[0], r[1]): (r[2], r[3]) for r in rows_b}
        known_stations = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows_m}
        # Facts another app has now reported are kept for good (retention skips pinned rows).
        to_pin = ([r[0] for r in rows_s if not r[3]], [(r[0], r[1]) for r in rows_b if not r[4]],
                  [r[0] for r in rows_m if not r[5]])
        snapshots: dict[int, list] = {}
        for market_id, observed_at, source, commodities in facts.lookup(
                conn, "SELECT market_id, observed_at, source, commodities FROM facts_market"
                      " WHERE market_id = ANY(%s)", markets):
            snapshots.setdefault(market_id, []).append((observed_at, source, commodities))

        results = []
        for mid, gw_ts, msg_ts, app_id, version, event_type, source, m, branch in messages:
            try:
                # Body, station and market facts are live-game only (facts.Batch.add): Legacy
                # messages are compared on systems alone (the same galaxy in both).
                live = branch != "legacy"
                pairs = compare_all(m, event_type, gw_ts, msg_ts, source, known_systems,
                                    known_bodies if live else {}, known_stations if live else {},
                                    snapshots if live else {}, by_name)
            except Exception:   # sender-controlled JSON: one odd message must never stall pairing
                log.exception("pairing skipped message %s", mid)
                continue
            for kind, result in pairs.items():
                if result:
                    compared, mismatched, out = result
                    results.append((mid, gw_ts, app_id, version, event_type, kind, compared,
                                    mismatched, json.dumps(out, default=str) if out else None))
        with conn.transaction():
            if results:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO pair_results (message_id, gw_ts, app_id, software_version,
                               event_type, pair_kind, compared, mismatched, mismatches)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """, results)
            facts.pin(conn, *to_pin)
            _set_state(conn, "pairing_last_id", rows[-1][0])
        done += len(rows)
    return done


PAIRING_LOCK = 0x45444E50   # advisory lock id: one pairing run at a time (worker or pair-backlog)


def run(conn: psycopg.Connection, max_seconds: float = MAX_SECONDS, wait: bool = False) -> None:
    """One pairing run. The worker skips if another run holds the lock; pair-backlog waits."""
    if wait:
        conn.execute("SELECT pg_advisory_lock(%s)", (PAIRING_LOCK,))
    elif not conn.execute("SELECT pg_try_advisory_lock(%s)", (PAIRING_LOCK,)).fetchone()[0]:
        log.info("pairing: another pairing run is active; skipped")
        return
    try:
        _run(conn, max_seconds)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (PAIRING_LOCK,))


def _run(conn: psycopg.Connection, max_seconds: float) -> None:
    heartbeat = _get_state(conn, "archive_import_heartbeat")
    if heartbeat and time.time() - heartbeat < HEARTBEAT_STALE:
        # An archive import is filling facts and messages; pairing now would compare
        # against half-built facts and move past those messages for good.
        log.info("pairing: paused while an archive import runs")
        return
    deadline = time.monotonic() + max_seconds
    n_facts = n_paired = 0
    facts_deadline = deadline - max_seconds / 3
    while True:
        folded, caught_up = update_facts(conn, facts_deadline)
        n_facts += folded
        if not caught_up:
            # Pairing against half-built facts would miss pairs for good: wait for the next run.
            log.info("pairing: %d reference messages folded into facts; facts still catching up",
                     n_facts)
            break
        # Rounds: pair what had arrived when the facts were brought up to date, then refresh the
        # facts before the next round, so a long backlog run never pairs late arrivals against
        # facts hours old (and market snapshots that have since expired).
        top = conn.execute("SELECT max(id) FROM messages WHERE NOT is_big3").fetchone()[0] or 0
        paired = pair_messages(conn, deadline, upto=top)
        n_paired += paired
        if paired < CHUNK or time.monotonic() >= deadline:
            break
        facts_deadline = min(deadline, time.monotonic() + max_seconds / 3)
    log.info("pairing: %d reference messages folded into facts, %d other messages checked",
             n_facts, n_paired)
