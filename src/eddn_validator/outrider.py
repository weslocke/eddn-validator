"""ED Outrider usage statistics for the owner's private page.

Carried over from the earlier standalone listener script:
totals of own and other players' messages, and an estimate of how many
commanders sent the other players' messages. EDDN's uploader hash changes every
few minutes, so people cannot be counted directly; instead a day's positions
are linked into one commander when they are within LINK_LY of each other,
within NEAR_LY and NEAR_S of each other (a neutron route), or share a hash.
Two players flying close together count as one; one who relogs far away as two.

Days are the owner's local days (America/New_York).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

import psycopg

log = logging.getLogger(__name__)

OUTRIDER = "ED Outrider"
LOCAL = ZoneInfo("America/New_York")
LINK_LY = 300   # light years: two positions of one day this close are one commander...
NEAR_LY = 800   # ...or this close within NEAR_S seconds of each other...
NEAR_S = 600    # ...or sharing EDDN's uploader hash


def position(message: object) -> tuple[float, float, float, str | None] | None:
    """(x, y, z, system) a message says the sender was at: StarPos, else a route's start."""
    if not isinstance(message, dict):
        return None
    p = message.get("StarPos")
    name = (message.get("StarSystem") or message.get("System") or message.get("systemName")
            or message.get("SystemName"))
    route = message.get("Route")
    if not (isinstance(p, list) and len(p) == 3) and isinstance(route, list) and route:
        first = route[0] if isinstance(route[0], dict) else {}
        p, name = first.get("StarPos"), first.get("StarSystem")
    if (isinstance(p, list) and len(p) == 3
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
        return float(p[0]), float(p[1]), float(p[2]), name
    return None


def group_commanders(points: list[tuple[float, str | None, tuple[float, float, float],
                                        str | None, dt.datetime]]) -> list[dict]:
    """Group (epoch, uploader, xyz, system, received) points into estimated commanders.

    Returns [{"where", "messages", "last"}], the busiest first.
    """
    parent = list(range(len(points)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    last_of: dict[str | None, int] = {}
    for i, (_, uploader, _, _, _) in enumerate(points):   # one hash: one commander
        if uploader is not None:
            if uploader in last_of:
                parent[root(i)] = root(last_of[uploader])
            last_of[uploader] = i

    # Pairs only among neighbouring cells of a NEAR_LY grid; NEAR_LY >= LINK_LY, so no link is missed.
    cells: dict[tuple[int, int, int], list[int]] = {}
    for i, p in enumerate(points):
        cells.setdefault(tuple(int(v // NEAR_LY) for v in p[2]), []).append(i)
    for (cx, cy, cz), members in cells.items():
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                for j in cells.get((cx + dx, cy + dy, cz + dz), ())]
        for i in members:
            for j in near:
                if j <= i:
                    continue
                d2 = sum((a - b) ** 2 for a, b in zip(points[i][2], points[j][2]))
                if d2 <= LINK_LY ** 2 or (abs(points[j][0] - points[i][0]) <= NEAR_S
                                          and d2 <= NEAR_LY ** 2):
                    parent[root(i)] = root(j)

    groups: dict[int, dict] = {}
    for i, (_, _, _, name, received) in enumerate(points):
        g = groups.setdefault(root(i), {"messages": 0, "last": None, "where": None})
        g["messages"] += 1
        if g["last"] is None or received >= g["last"]:
            g["last"], g["where"] = received, name or g["where"]
    return sorted(groups.values(), key=lambda g: -g["messages"])


def day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """One local day (midnight to midnight in New York, DST and all) as UTC datetimes."""
    start = dt.datetime.combine(day, dt.time(), LOCAL)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), LOCAL)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


# Finished days' commander estimates are kept for an hour between requests (not forever:
# mark-mine and imports can still change a recent day after it ends).
PAST_DAY_TTL = 3600
_past_days: dict[tuple[int, dt.date], tuple[float, tuple[int, list[dict]]]] = {}


class Stats:
    """Queries for the page. `mine IS NOT TRUE` (other players or unknown) counts as
    other players, as the earlier listener did."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        row = conn.execute("SELECT id FROM apps WHERE software_name = %s", (OUTRIDER,)).fetchone()
        self.app_id = row[0] if row else None

    def totals(self) -> dict:
        row = self.conn.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE mine), count(*) FILTER (WHERE NOT mine),
                   count(*) FILTER (WHERE mine IS NULL), min(gw_ts),
                   max(gw_ts) FILTER (WHERE mine IS NOT TRUE)
              FROM messages WHERE app_id = %s
            """, (self.app_id,)).fetchone()
        keys = ("total", "mine", "others", "unknown", "first", "last_other")
        return dict(zip(keys, row))

    def commanders(self, day: dt.date) -> tuple[int, list[dict]]:
        """Other players' messages on one local day and the estimated commanders."""
        start, end = day_bounds(day)
        rows = self.conn.execute(
            """
            SELECT gw_ts, payload->'header'->>'uploaderID', payload->'message'
              FROM messages
             WHERE app_id = %s AND mine IS NOT TRUE AND gw_ts >= %s AND gw_ts < %s
             ORDER BY gw_ts
            """, (self.app_id, start, end)).fetchall()
        points = []
        for gw_ts, uploader, message in rows:
            p = position(message)
            if p:
                points.append((gw_ts.timestamp(), uploader, p[:3], p[3], gw_ts))
        return len(rows), group_commanders(points)

    def daily(self, days: int, today_result: tuple[dt.date, int, list[dict]] | None = None) -> list[dict]:
        """The last `days` local days, newest first: messages (own and other) and commanders.
        today_result: (that day, *commanders(that day)), when the caller already has it."""
        today = dt.datetime.now(LOCAL).date()
        first = today - dt.timedelta(days=days - 1)
        counts = {
            d: (mine, others)
            for d, mine, others in self.conn.execute(
                """
                SELECT (gw_ts AT TIME ZONE 'America/New_York')::date AS d,
                       count(*) FILTER (WHERE mine), count(*) FILTER (WHERE mine IS NOT TRUE)
                  FROM messages
                 WHERE app_id = %s AND gw_ts >= %s
                 GROUP BY d
                """, (self.app_id, day_bounds(first)[0]))
        }
        # Finished days come from the nightly table (refresh_days); only a day it lacks is
        # worked out here, and kept for an hour.
        stored = dict(self.conn.execute(
            "SELECT day, commanders FROM outrider_days WHERE app_id = %s AND day >= %s AND day < %s",
            (self.app_id, first, today)).fetchall())
        out = []
        for n in range(days):
            day = today - dt.timedelta(days=n)
            key = (self.app_id, day)
            cached = _past_days.get(key)
            if today_result is not None and day == today_result[0]:
                commanders = len(today_result[2])
            elif day in stored:
                commanders = stored[day]
            elif day != today and cached and time.monotonic() - cached[0] < PAST_DAY_TTL:
                commanders = len(cached[1][1])
            else:
                result = self.commanders(day)
                if day != today:
                    _past_days[key] = (time.monotonic(), result)
                commanders = len(result[1])
            mine, others = counts.get(day, (0, 0))
            out.append({"day": day, "mine": mine, "others": others, "commanders": commanders})
        return out

    def versions(self, since: dt.datetime) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT software_version, count(*) FILTER (WHERE mine IS NOT TRUE),
                   count(*) FILTER (WHERE mine), min(gw_ts), max(gw_ts)
              FROM messages WHERE app_id = %s AND gw_ts >= %s
             GROUP BY software_version ORDER BY max(gw_ts) DESC
            """, (self.app_id, since)).fetchall()
        return [dict(zip(("version", "others", "mine", "first", "last"), r)) for r in rows]

    def event_types(self, since: dt.datetime) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT event_type, is_test, count(*) FILTER (WHERE mine IS NOT TRUE),
                   count(*) FILTER (WHERE mine), max(gw_ts)
              FROM messages WHERE app_id = %s AND gw_ts >= %s
             GROUP BY event_type, is_test ORDER BY 3 DESC, 4 DESC
            """, (self.app_id, since)).fetchall()
        return [dict(zip(("event_type", "is_test", "others", "mine", "last"), r)) for r in rows]


# Days recomputed every night: messages can arrive late (apps sending old journals, EDDN
# catching up after an outage) and mark-mine can still judge them, so a week is redone.
RECOMPUTE_DAYS = 7
NIGHTLY_HOUR = 3            # local (New York) hour after which the nightly run happens
NIGHTLY_STATE = "outrider_days_run"


def refresh_days(conn: psycopg.Connection, recompute: int = RECOMPUTE_DAYS) -> int:
    """Store finished days up to yesterday: every day not stored yet, and the last `recompute`
    days again. Returns the number of days computed."""
    stats = Stats(conn)
    if stats.app_id is None:
        return 0
    first = conn.execute("SELECT min(gw_ts) FROM messages WHERE app_id = %s", (stats.app_id,)).fetchone()[0]
    if first is None:
        return 0
    today = dt.datetime.now(LOCAL).date()
    have = {d for (d,) in conn.execute("SELECT day FROM outrider_days WHERE app_id = %s", (stats.app_id,))}
    day, todo = first.astimezone(LOCAL).date(), []
    while day < today:
        if day not in have or day >= today - dt.timedelta(days=recompute):
            todo.append(day)
        day += dt.timedelta(days=1)
    for day in todo:
        messages, groups = stats.commanders(day)
        conn.execute(
            """
            INSERT INTO outrider_days (app_id, day, messages, commanders, computed_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (app_id, day) DO UPDATE SET messages = EXCLUDED.messages,
                commanders = EXCLUDED.commanders, computed_at = EXCLUDED.computed_at
            """, (stats.app_id, day, messages, len(groups)))
    return len(todo)


def nightly(conn: psycopg.Connection) -> None:
    """The worker calls this hourly; it refreshes the stored days once a day, after 3 AM."""
    now = dt.datetime.now(LOCAL)
    stamp = int(now.strftime("%Y%m%d"))
    done = conn.execute("SELECT value FROM job_state WHERE name = %s", (NIGHTLY_STATE,)).fetchone()
    if now.hour < NIGHTLY_HOUR or (done and done[0] >= stamp):
        return
    n = refresh_days(conn)
    conn.execute("INSERT INTO job_state (name, value) VALUES (%s, %s) ON CONFLICT (name)"
                 " DO UPDATE SET value = EXCLUDED.value, updated_at = now()", (NIGHTLY_STATE, stamp))
    log.info("outrider days: %d days stored", n)
