"""Reference facts: what the big four say about systems, bodies, stations and markets.

Built incrementally from reference-app messages (any of their versions, live
schemas only) so that later messages from other apps can be paired with them
(docs/DESIGN.md §6.4). Only static data is kept long-term; market snapshots are
kept just long enough for the pairing window.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

log = logging.getLogger(__name__)

# Scan fields that do not change over time. Excluded on purpose: MeanAnomaly,
# DistanceFromArrivalLS (orbital position), WasDiscovered/WasMapped/WasFootfalled
# (who got there first), ScanType (how it was scanned).
BODY_STATIC = (
    "StarType", "Subclass", "StellarMass", "AbsoluteMagnitude", "Luminosity", "Age_MY",
    "PlanetClass", "MassEM", "Radius", "SurfaceGravity", "SurfaceTemperature", "SurfacePressure",
    "Atmosphere", "AtmosphereType", "AtmosphereComposition", "Composition", "Materials", "Rings",
    "RotationPeriod", "AxialTilt", "SemiMajorAxis", "Eccentricity", "OrbitalInclination",
    "Periapsis", "OrbitalPeriod", "AscendingNode", "TidalLock", "Landable", "Volcanism",
    "TerraformState", "ReserveLevel", "Parents",
)
POS_TOLERANCE = 1 / 64   # StarPos is on a 1/32 ly grid
REL_TOLERANCE = 1e-6


def canonical(value: object) -> object:
    """Order-independent form for comparing lists of objects (Materials, Rings, ...)."""
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    return value


def same(a: object, b: object) -> bool:
    """Equal, with numbers compared to a small relative tolerance and strings exactly."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int) and isinstance(b, int):
        return a == b   # exact; also avoids float overflow on huge sender-supplied integers
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return math.isclose(a, b, rel_tol=REL_TOLERANCE, abs_tol=1e-9)
        except OverflowError:
            return False
    if isinstance(a, list) and isinstance(b, list):
        a, b = canonical(a), canonical(b)
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    return a == b


GALAXY_LIMIT_LY = 1_000_000   # far beyond the galaxy (about +-50,000 ly); anything larger is garbage


def valid_pos(pos: object) -> tuple[float, float, float] | None:
    """A StarPos as three finite floats inside a generous galaxy bound, else None."""
    if not (isinstance(pos, list) and len(pos) == 3):
        return None
    out = []
    for v in pos:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        try:
            f = float(v)
        except OverflowError:
            return None
        if not math.isfinite(f) or abs(f) > GALAXY_LIMIT_LY:
            return None
        out.append(f)
    return out[0], out[1], out[2]


def same_pos(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all(abs(x - y) <= POS_TOLERANCE for x, y in zip(a, b))


INT64_MAX = 2**63 - 1
INT32_MAX = 2**31 - 1


def db_int(value: object, limit: int = INT64_MAX) -> int | None:
    """value as a non-negative integer the database column can hold, else None
    (a sender's garbage must not fail a whole batch)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit:
        return value
    return None


def system_name(message: dict) -> object:
    for key in ("StarSystem", "System", "SystemName", "systemName"):
        if key in message:
            return message[key]
    return None


def systems_in(message: dict) -> list[tuple[int, str, tuple[float, float, float]]]:
    """(SystemAddress, name, StarPos) claims in a message, NavRoute hops included."""
    claims = []
    candidates = [(message.get("SystemAddress"), system_name(message), message.get("StarPos"))]
    for hop in message.get("Route") or []:
        if isinstance(hop, dict):
            candidates.append((hop.get("SystemAddress"), hop.get("StarSystem"), hop.get("StarPos")))
    for address, name, pos in candidates:
        p = valid_pos(pos)
        if db_int(address) is not None and isinstance(name, str) and p:
            claims.append((address, name, p))
    return claims


# Station types written differently by different senders. EDDiscovery calls an
# Ocellus "Bernal" and writes "Unknown" for types it does not know.
STATION_TYPE_ALIASES = {"bernal": "ocellus"}


def station_type_key(kind: str | None) -> str | None:
    if not kind or kind.lower() == "unknown":
        return None
    k = kind.lower()
    return STATION_TYPE_ALIASES.get(k, k)


def stable_station_name(name: str | None) -> str | None:
    """Names that are localisation symbols ($Operations_..., mission megaships) change: skip."""
    return None if not name or name.startswith("$") else name


# MarketIDs 127,000,000-127,999,999 are placeholders the game shares between many instanced
# "Operations" stations (mission megaships, rescue ships); below that no real station exists.
# Neither identifies a persistent station, so neither becomes a station fact.
OPERATION_IDS = range(127_000_000, 128_000_000)
FIRST_REAL_MARKET_ID = 128_000_000


def persistent_market(market: int | None) -> bool:
    return market is not None and market >= FIRST_REAL_MARKET_ID


# Stations that move between systems, by MarketID block (EDDiscovery's classification,
# Spansh's ranges): 3400xxxxxx rescue ships, 3600xxxxxx megaships, 3700-3939xxxxxx fleet
# carriers (some journals call them surface stations). Plus anything typed as such.
MOVING_TYPES = {"fleetcarrier", "megaship"}
CARRIER_RANGE = range(3_600_000_000, 3_940_000_000)


# Colonisation's temporary stations: construction depots, and the colonisation ships that the
# game types as SurfaceStation, all with IDs in 3.95-4.0e9. The game reuses their MarketIDs for
# new temporary stations elsewhere once a build finishes (2,332 depot IDs were reported in more
# than one system by reference apps in 30 days), and a planetary depot's ID passes to the
# station built there. A record of one says nothing reliable about a later report.
TEMPORARY_TYPES = {"spaceconstructiondepot", "planetaryconstructiondepot"}
COLONISATION_RANGE = range(3_950_000_000, 4_000_000_000)


def temporary_station(market: int, kind: str | None) -> bool:
    return kind in TEMPORARY_TYPES or market in COLONISATION_RANGE


def moving_station(market: int, kind: str | None) -> bool:
    """Whether a station can change system (so its system is not compared).

    Decided by type when it is known: carriers and megaships move, and so do PowerPlay
    Stronghold Carriers, which the game reports as SurfaceStation with IDs in the carrier
    range. Every other known type is fixed, whatever its ID: Odyssey settlements (3.8-3.94e9)
    and crater outposts (3.7-3.8e9) share the carrier ID blocks. The ID ranges decide only
    when the type is unknown (3.4e9 rescue ships, 3.6-3.94e9 megaships and carriers)."""
    key = station_type_key(kind)
    if key is not None:
        return key in MOVING_TYPES or (key == "surfacestation" and market in CARRIER_RANGE)
    return 3_400_000_000 <= market < 3_500_000_000 or market in CARRIER_RANGE


def station_system(event_type: str, message: dict) -> tuple[str | None, int | None]:
    """(system name, SystemAddress) a station-related message places the station in."""
    if event_type in ("commodity/3", "outfitting/2", "outfitting/3", "shipyard/2"):
        name, address = message.get("systemName"), None
    elif event_type in ("journal/1:Docked", "approachsettlement/1") or (
            event_type == "journal/1:Location" and message.get("Docked")):
        name, address = message.get("StarSystem"), message.get("SystemAddress")
    else:
        return None, None
    return (name if isinstance(name, str) and name else None), db_int(address)


def station_in(event_type: str, message: dict) -> tuple[int, str | None, str | None] | None:
    """(MarketID, station name, station type) a message states, if any."""
    if event_type in ("commodity/3", "outfitting/2", "outfitting/3", "shipyard/2"):
        market, name, kind = message.get("marketId"), message.get("stationName"), message.get("stationType")
    elif event_type == "approachsettlement/1":
        market, name, kind = message.get("MarketID"), message.get("Name"), None
    elif event_type in ("journal/1:Docked", "dockinggranted/1", "dockingdenied/1") or (
            event_type == "journal/1:Location" and message.get("Docked")):
        market, name, kind = message.get("MarketID"), message.get("StationName"), message.get("StationType")
    else:
        return None
    if not persistent_market(db_int(market)):
        return None
    return (market, stable_station_name(name) if isinstance(name, str) else None,
            station_type_key(kind) if isinstance(kind, str) else None)


def market_snapshot(message: dict) -> dict[str, list]:
    snapshot = {}
    for item in message.get("commodities") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            snapshot[item["name"].lower()] = [item.get("buyPrice"), item.get("sellPrice"),
                                              item.get("stock"), item.get("demand")]
    return snapshot


LOOKUP_SLICE = 10_000   # keys per lookup: above ~20k the planner turns to a seq scan of the table


def lookup(conn: psycopg.Connection, query: str, keys) -> list[tuple]:
    """Rows for `query` (one "= ANY(%s)" parameter) over keys, in slices of LOOKUP_SLICE."""
    keys, rows = list(keys), []
    for i in range(0, len(keys), LOOKUP_SLICE):
        rows += conn.execute(query, (keys[i:i + LOOKUP_SLICE],)).fetchall()
    return rows


def lookup_bodies(conn: psycopg.Connection, columns: str, keys) -> list[tuple]:
    """facts_body rows (the given columns) for (system_address, body_id) keys, sliced."""
    keys, rows = list(keys), []
    for i in range(0, len(keys), LOOKUP_SLICE):
        part = keys[i:i + LOOKUP_SLICE]
        rows += conn.execute(
            f"SELECT {columns} FROM facts_body WHERE (system_address, body_id) IN"
            " (SELECT * FROM unnest(%s::bigint[], %s::int[]))",
            ([k[0] for k in part], [k[1] for k in part])).fetchall()
    return rows


def pin(conn: psycopg.Connection, systems, bodies, stations) -> None:
    """Mark facts as needed by another app's report, so retention keeps them. Keys sorted, so
    concurrent writers lock rows in one order. Runs inside the caller's transaction; for
    millions of keys use pin_in_slices."""
    for i in range(0, len(systems := sorted(systems)), LOOKUP_SLICE):
        conn.execute("UPDATE facts_system SET pinned = true WHERE system_address = ANY(%s)"
                     " AND NOT pinned", (systems[i:i + LOOKUP_SLICE],))
    for i in range(0, len(bodies := sorted(bodies)), LOOKUP_SLICE):
        part = bodies[i:i + LOOKUP_SLICE]
        conn.execute("UPDATE facts_body SET pinned = true WHERE (system_address, body_id) IN"
                     " (SELECT * FROM unnest(%s::bigint[], %s::int[])) AND NOT pinned",
                     ([k[0] for k in part], [k[1] for k in part]))
    for i in range(0, len(stations := sorted(stations)), LOOKUP_SLICE):
        conn.execute("UPDATE facts_station SET pinned = true WHERE market_id = ANY(%s) AND NOT pinned",
                     (stations[i:i + LOOKUP_SLICE],))


def pin_in_slices(conn: psycopg.Connection, systems, bodies, stations, retries: int = 5) -> None:
    """pin() for very large key sets: one short transaction per slice (idempotent through
    "AND NOT pinned"), retried on a deadlock, so a failure costs one slice, not the lot."""
    for kind, keys in (("s", sorted(systems)), ("b", sorted(bodies)), ("m", sorted(stations))):
        for i in range(0, len(keys), LOOKUP_SLICE):
            part = keys[i:i + LOOKUP_SLICE]
            args = (part, [], []) if kind == "s" else ([], part, []) if kind == "b" else ([], [], part)
            for attempt in range(retries):
                try:
                    with conn.transaction():
                        pin(conn, *args)
                    break
                except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                    if attempt == retries - 1:
                        raise
                    time.sleep(2 * (attempt + 1))


CONFLICT_HEAL_OBSERVATIONS = 3   # agreeing observations in one batch that clear a dispute


@dataclass
class Batch:
    systems: dict[int, dict] = field(default_factory=dict)
    bodies: dict[tuple[int, int], dict] = field(default_factory=dict)
    stations: dict[int, dict] = field(default_factory=dict)
    markets: list[tuple] = field(default_factory=list)

    def add(self, event_type: str, message: dict, branch: str, source: str, ref_group: str,
            gw_ts: datetime, msg_ts: datetime | None) -> None:
        for address, name, pos in systems_in(message):
            if name != "Sol" and all(v == 0 for v in pos):
                continue    # the [0,0,0] placeholder (GEN-032) must not dispute a real position
            s = self.systems.setdefault(address, {"names": set(), "positions": [], "groups": set(),
                                                  "n": 0, "last": gw_ts})
            s["names"].add(name)
            if not any(same_pos(pos, p) for p in s["positions"]):
                s["positions"].append(pos)
            s["groups"].add(ref_group)
            s["n"] += 1
            s["last"] = max(s["last"], gw_ts)
        # Bodies only from the live game: Legacy (3.8) planets differ from Odyssey's, so mixing
        # them would mark real details as disputed. Systems are the same galaxy in both.
        if event_type == "journal/1:Scan" and branch == "live":
            address = db_int(message.get("SystemAddress"))
            body_id = db_int(message.get("BodyID"), INT32_MAX)
            if address is not None and body_id is not None:
                b = self.bodies.setdefault((address, body_id), {"name": None, "static": {},
                                                                "conflicts": set(), "n": 0,
                                                                "last": gw_ts})
                if isinstance(message.get("BodyName"), str):
                    b["name"] = message["BodyName"]
                merge_static(b, {k: message[k] for k in BODY_STATIC if k in message})
                b["n"] += 1
                b["last"] = max(b["last"], gw_ts)
        if branch == "live":
            station = station_in(event_type, message)
            if station:
                market, name, kind = station
                st = self.stations.setdefault(market, {"name": None, "type": None, "n": 0,
                                                       "last": gw_ts, "system": None,
                                                       "address": None, "system_seen": None,
                                                       "since": gw_ts})
                if gw_ts >= st["last"] or st["name"] is None:
                    if station_changed(st["name"], st["type"], name, kind):
                        st["since"] = gw_ts     # changed within the batch: the new state starts here
                    st["name"] = name or st["name"]
                    st["type"] = kind or st["type"]
                system, address = station_system(event_type, message)
                if system and (st["system_seen"] is None or gw_ts >= st["system_seen"]):
                    st["system"], st["system_seen"] = system, gw_ts
                    st["address"] = address or st["address"]
                st["n"] += 1
                st["last"] = max(st["last"], gw_ts)
        if event_type == "commodity/3" and branch == "live" and msg_ts is not None:
            market = db_int(message.get("marketId"))
            if market is not None:
                self.markets.append((market, msg_ts, source, ref_group,
                                     json.dumps(market_snapshot(message))))


def merge_static(body: dict, new: dict) -> None:
    for key, value in new.items():
        if key in body["conflicts"]:
            continue
        if key in body["static"] and not same(body["static"][key], value):
            body["conflicts"].add(key)
            del body["static"][key]
        else:
            body["static"][key] = value


def store(conn: psycopg.Connection, batch: Batch) -> None:
    """Merge a batch into the fact tables (the worker is the only writer)."""
    if batch.systems:
        existing = {row[0]: row for row in lookup(
            conn, "SELECT system_address, star_system, star_pos, obs_count, ref_groups, conflict,"
                  " last_seen FROM facts_system WHERE system_address = ANY(%s)", batch.systems)}
        rows = []
        # Sorted, so concurrent writers (import workers, the worker, pin()) lock rows in one order.
        for address, s in sorted(batch.systems.items()):
            names, positions, groups = set(s["names"]), list(s["positions"]), set(s["groups"])
            n, last, conflict = s["n"], s["last"], False
            if address in existing:
                _, old_name, old_pos, old_n, old_groups, old_conflict, old_last = existing[address]
                groups |= set(old_groups)
                n += old_n
                last = max(last, old_last)
                restores = (old_name in names and any(same_pos(tuple(old_pos), p) for p in positions))
                if old_conflict and len(names) == 1 and len(positions) == 1 \
                        and s["n"] >= CONFLICT_HEAL_OBSERVATIONS \
                        and (len(s["groups"]) >= 2 or restores):
                    # Disputed before, but the newest observations all agree, and either two
                    # reference apps agree or they confirm the value already held: one stray
                    # report must not remove a system from pairing for good, while one app's
                    # newcomer value must not flip a genuinely disputed system back and forth.
                    conflict = False
                else:
                    names.add(old_name)
                    if not any(same_pos(tuple(old_pos), p) for p in positions):
                        positions.append(tuple(old_pos))
                    conflict = old_conflict
            conflict = conflict or len(names) > 1 or len(positions) > 1
            rows.append((address, sorted(names)[0], list(positions[0]), n, sorted(groups),
                         conflict, last))
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO facts_system (system_address, star_system, star_pos, obs_count,
                                          ref_groups, conflict, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (system_address) DO UPDATE SET
                    star_system = EXCLUDED.star_system, star_pos = EXCLUDED.star_pos,
                    obs_count = EXCLUDED.obs_count, ref_groups = EXCLUDED.ref_groups,
                    conflict = EXCLUDED.conflict, last_seen = EXCLUDED.last_seen
                """, rows)

    if batch.bodies:
        keys = list(batch.bodies)
        existing = {(row[0], row[1]): row for row in lookup_bodies(
            conn, "system_address, body_id, body_name, static, obs_count, conflict_fields, last_seen",
            keys)}
        rows = []
        for key, b in sorted(batch.bodies.items()):
            merged = {"name": b["name"], "static": {}, "conflicts": set(), "n": 0, "last": b["last"]}
            if key in existing:
                _, _, old_name, old_static, old_n, old_conflicts, old_last = existing[key]
                merged.update(name=b["name"] or old_name, conflicts=set(old_conflicts),
                              n=old_n, last=max(b["last"], old_last))
                merge_static(merged, old_static)
            merged["conflicts"] |= b["conflicts"]
            merge_static(merged, b["static"])
            for k in merged["conflicts"]:
                merged["static"].pop(k, None)
            rows.append((key[0], key[1], merged["name"], json.dumps(merged["static"]),
                         merged["n"] + b["n"], sorted(merged["conflicts"]), merged["last"]))
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO facts_body (system_address, body_id, body_name, static, obs_count,
                                        conflict_fields, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (system_address, body_id) DO UPDATE SET
                    body_name = EXCLUDED.body_name, static = EXCLUDED.static,
                    obs_count = EXCLUDED.obs_count, conflict_fields = EXCLUDED.conflict_fields,
                    last_seen = EXCLUDED.last_seen
                """, rows)

    if batch.stations:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO facts_station (market_id, station_name, station_type, obs_count, last_seen,
                                           star_system, system_address, system_seen, state_since)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (market_id) DO UPDATE SET
                    -- A newer report naming or typing the station differently is a change: keep the
                    -- old state and when each was seen (SET reads the row as it was before).
                    prev_station_name = CASE WHEN {changed} THEN facts_station.station_name
                                             ELSE facts_station.prev_station_name END,
                    prev_station_type = CASE WHEN {changed} THEN facts_station.station_type
                                             ELSE facts_station.prev_station_type END,
                    prev_seen_until = CASE WHEN {changed} THEN facts_station.last_seen
                                           ELSE facts_station.prev_seen_until END,
                    state_since = CASE WHEN {changed} THEN EXCLUDED.state_since
                                       ELSE facts_station.state_since END,
                    star_system = CASE WHEN EXCLUDED.system_seen >= coalesce(facts_station.system_seen, '-infinity')
                                       THEN coalesce(EXCLUDED.star_system, facts_station.star_system)
                                       ELSE facts_station.star_system END,
                    system_address = CASE WHEN EXCLUDED.system_seen >= coalesce(facts_station.system_seen, '-infinity')
                                          THEN coalesce(EXCLUDED.system_address, facts_station.system_address)
                                          ELSE facts_station.system_address END,
                    system_seen = GREATEST(facts_station.system_seen, EXCLUDED.system_seen),
                    -- a missing name/type (stations from the targeted pull) is filled from any report
                    station_name = CASE WHEN facts_station.station_name IS NULL
                                             OR EXCLUDED.last_seen >= facts_station.last_seen
                                        THEN coalesce(EXCLUDED.station_name, facts_station.station_name)
                                        ELSE facts_station.station_name END,
                    station_type = CASE WHEN facts_station.station_type IS NULL
                                             OR EXCLUDED.last_seen >= facts_station.last_seen
                                        THEN coalesce(EXCLUDED.station_type, facts_station.station_type)
                                        ELSE facts_station.station_type END,
                    obs_count = facts_station.obs_count + EXCLUDED.obs_count,
                    last_seen = GREATEST(facts_station.last_seen, EXCLUDED.last_seen)
                """.replace("{changed}", STATION_CHANGED),
                [(m, s["name"], s["type"], s["n"], s["last"], s["system"], s["address"],
                  s["system_seen"], s["since"]) for m, s in sorted(batch.stations.items())])

    if batch.markets:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO facts_market (market_id, observed_at, source, ref_group, commodities)"
                " VALUES (%s, %s, %s, %s, %s)", batch.markets)


# A change of name always counts; a change of type only when neither is the generic
# "surfacestation", which the game also uses for carriers and other stations it otherwise types.
STATION_CHANGED = """(EXCLUDED.last_seen >= facts_station.last_seen AND (
    (EXCLUDED.station_name IS NOT NULL AND facts_station.station_name IS NOT NULL
     AND lower(EXCLUDED.station_name) <> lower(facts_station.station_name))
    OR (EXCLUDED.station_type IS NOT NULL AND facts_station.station_type IS NOT NULL
        AND EXCLUDED.station_type <> facts_station.station_type
        AND 'surfacestation' NOT IN (EXCLUDED.station_type, facts_station.station_type))))"""


def station_changed(old_name: str | None, old_kind: str | None, name: str | None, kind: str | None) -> bool:
    """STATION_CHANGED for one pair of states (type keys)."""
    if old_name and name and old_name.lower() != name.lower():
        return True
    return bool(old_kind and kind and old_kind != kind and "surfacestation" not in (old_kind, kind))


def station_state_at(t, fact: tuple) -> tuple[str | None, str | None, object] | None:
    """The (name, type, seen) a station record says the station had at time t, or None when t
    falls where the state is unknown: between a change's last old report and first new one, or
    in an overlap of the two. fact: (name, type, last_seen, prev_name, prev_type,
    prev_seen_until, state_since)."""
    name, kind, last_seen, prev_name, prev_kind, prev_until, since = fact
    if prev_until is None or since is None or (t >= since and t >= prev_until):
        return name, kind, last_seen
    if t < since and t <= prev_until:
        return prev_name, prev_kind, prev_until
    return None


BACKFILL_STATION_SYSTEMS = """
UPDATE facts_station f
   SET star_system = x.sys, system_address = x.addr, system_seen = x.ts
  FROM (SELECT DISTINCT ON (mid) mid, sys, addr, ts FROM (
            SELECT (payload->'message'->>'MarketID')::numeric AS mid,
                   payload->'message'->>'StarSystem' AS sys,
                   CASE WHEN jsonb_typeof(payload->'message'->'SystemAddress') = 'number'
                        THEN (payload->'message'->>'SystemAddress')::numeric END AS addr,
                   gw_ts AS ts
              FROM messages
             WHERE is_big3 AND app_id = ANY(%(ref_ids)s) AND NOT is_test
               AND jsonb_typeof(payload->'message'->'MarketID') = 'number'
               AND (event_type IN ('journal/1:Docked', 'approachsettlement/1')
                    OR (event_type = 'journal/1:Location' AND payload->'message'->>'Docked' = 'true'))
        ) y
        WHERE mid >= 128000000 AND mid < 9223372036854775807 AND sys IS NOT NULL
          AND (addr IS NULL OR addr < 9223372036854775807)
        ORDER BY mid, ts DESC) x
 WHERE f.market_id = x.mid AND (f.system_seen IS NULL OR f.system_seen < x.ts)
"""


def backfill_station_systems(conn: psycopg.Connection) -> int:
    """Fill station systems from all stored reference-app Docked/Location/settlement messages."""
    # The reference apps' ids let messages_big3_idx (app_id first) find their rows instead of
    # a sequential scan of every partition.
    ref_ids = [r[0] for r in conn.execute("SELECT id FROM apps WHERE ref_group IS NOT NULL")]
    with conn.transaction():
        return conn.execute(BACKFILL_STATION_SYSTEMS, {"ref_ids": ref_ids}).rowcount
