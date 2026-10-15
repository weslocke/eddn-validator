"""Reference profiles and behaviour baselines (layers 2, 3 and 5; docs/DESIGN.md §6.3, §6.6).

Rebuilt hourly from the retained messages of the reference apps' current
versions (live schemas only). A profile is kept per (event_type, subtype,
branch, reference group), plus a '*' subtype for the whole event type, which
the scorer falls back to when a subtype has too little reference data.
"""

from __future__ import annotations

import json
import logging
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone

import psycopg

from .paths import flatten, string_shape
from .rules import Rule, evaluate
from .subtypes import subtype

log = logging.getLogger(__name__)

PER_BUCKET = 2000        # newest reference messages per (app, event type, branch)
NUM_SAMPLES = 2000       # numbers kept per path for percentiles
MAX_DISTINCT = 200       # more distinct strings than this: not an enum
VALUES_PER_MESSAGE = 50  # scalar values looked at per path per message


class Acc:
    """Accumulates one bucket's statistics."""

    def __init__(self) -> None:
        self.n = 0
        self.present: Counter[str] = Counter()
        self.types: dict[str, Counter[str]] = defaultdict(Counter)
        self.nums: dict[str, list[float]] = defaultdict(list)
        self.num_seen: Counter[str] = Counter()
        self.integral: Counter[str] = Counter()
        self.strs: dict[str, Counter[str]] = defaultdict(Counter)
        self.str_overflow: set[str] = set()
        self.str_seen: Counter[str] = Counter()
        self.shapes: dict[str, Counter[str]] = defaultdict(Counter)
        self.lens: dict[str, list[int]] = defaultdict(list)

    def add(self, message: dict) -> None:
        flat = flatten(message)
        self.n += 1
        for path, types in flat.types.items():
            self.present[path] += 1
            self.types[path].update(types)
        for path, values in flat.scalars.items():
            for v in values[:VALUES_PER_MESSAGE]:
                if isinstance(v, bool) or v is None:
                    continue
                if isinstance(v, (int, float)):
                    try:
                        fv = float(v)
                    except OverflowError:   # absurd sender-supplied integer
                        continue
                    if not math.isfinite(fv):
                        continue
                    self.num_seen[path] += 1
                    if fv.is_integer():
                        self.integral[path] += 1
                    bucket = self.nums[path]
                    if len(bucket) < NUM_SAMPLES:
                        bucket.append(fv)
                    else:   # reservoir sampling keeps the sample unbiased
                        j = random.randrange(self.num_seen[path])
                        if j < NUM_SAMPLES:
                            bucket[j] = fv
                elif isinstance(v, str):
                    self.str_seen[path] += 1
                    self.shapes[path][string_shape(v)] += 1
                    if path not in self.str_overflow:
                        counter = self.strs[path]
                        counter[v] += 1
                        if len(counter) > MAX_DISTINCT:
                            self.str_overflow.add(path)
                            del self.strs[path]
        for path, lengths in flat.lengths.items():
            if len(self.lens[path]) < NUM_SAMPLES:
                self.lens[path].extend(lengths[:VALUES_PER_MESSAGE])

    def profile(self) -> dict:
        paths = {}
        for path, count in self.present.items():
            entry: dict = {
                "freq": round(count / self.n, 4),
                "types": dict(self.types[path]),
            }
            nums = sorted(self.nums.get(path, []))
            if nums:
                entry["num"] = {
                    "min": nums[0], "max": nums[-1],
                    "p01": nums[int(0.01 * (len(nums) - 1))],
                    "p99": nums[int(0.99 * (len(nums) - 1))],
                    "integral": round(self.integral[path] / self.num_seen[path], 4),
                    "samples": self.num_seen[path],
                }
            if self.str_seen.get(path):
                entry["str"] = {
                    "samples": self.str_seen[path],
                    "values": None if path in self.str_overflow else dict(self.strs[path]),
                    "shapes": dict(self.shapes[path].most_common(12)),
                }
            lens = sorted(self.lens.get(path, []))
            if lens:
                entry["len"] = {"min": lens[0], "max": lens[-1],
                                "p99": lens[int(0.99 * (len(lens) - 1))]}
            paths[path] = entry
        return {"n": self.n, "paths": paths}


# Buckets to sample: every (reference app, event type, branch) seen lately or
# already profiled. Each is then read newest-first through messages_big3_idx, so
# the cost does not grow with the months of reference history kept.
BUCKETS = """
SELECT DISTINCT m.app_id, a.ref_group, m.event_type, m.branch
  FROM messages m JOIN apps a ON a.id = m.app_id
 WHERE m.is_big3 AND NOT m.is_test AND m.gw_ts > now() - interval '2 days'
UNION
SELECT a.id, a.ref_group, rp.event_type, rp.branch
  FROM ref_profiles rp JOIN apps a ON a.ref_group = rp.ref_group
 WHERE rp.subtype = '*'
"""

SAMPLE = """
SELECT m.event_type, m.branch, m.source, m.payload->'message', m.payload
  FROM messages m
 WHERE m.is_big3 AND m.app_id = %s AND m.event_type = %s AND m.branch = %s AND NOT m.is_test
   AND m.software_version = ANY(%s) AND m.gw_ts > now() - interval '30 days'
 ORDER BY m.gw_ts DESC
 LIMIT %s
"""

# Same definitions as scorer._counts_and_behaviour, from the same scoring.yaml values. A duplicate is
# a message whose identical copy (same app, same content) arrived within the previous
# duplicate_window_hours; the extra day read before the 2-day window lets the first messages
# in it see their earlier copies.
BASELINE = """
SELECT ref_group, event_type, count(*),
       count(*) FILTER (WHERE gap <= make_interval(hours => %(dup_hours)s)),
       count(*) FILTER (WHERE gw_ts - msg_ts > make_interval(mins => %(stale)s)),
       count(*) FILTER (WHERE msg_ts > gw_ts + make_interval(mins => %(future)s))
  FROM (SELECT a.ref_group, m.event_type, m.gw_ts, m.msg_ts,
               m.gw_ts - lag(m.gw_ts) OVER (PARTITION BY m.app_id, m.content_hash ORDER BY m.gw_ts) AS gap
          FROM messages m JOIN apps a ON a.id = m.app_id
         WHERE m.is_big3 AND NOT m.is_test
           AND m.gw_ts > now() - interval '2 days' - make_interval(hours => %(dup_hours)s)) x
 WHERE gw_ts > now() - interval '2 days'
 GROUP BY ref_group, event_type
"""

BURSTS = """
SELECT ref_group, event_type, sum(c) FROM (
    SELECT a.ref_group, m.event_type, count(*) AS c
      FROM messages m JOIN apps a ON a.id = m.app_id
     WHERE m.is_big3 AND NOT m.is_test AND m.gw_ts > now() - interval '2 days'
     GROUP BY a.ref_group, m.app_id, m.software_version, m.event_type, date_trunc('second', m.gw_ts)
    HAVING count(*) >= %s
) b GROUP BY ref_group, event_type
"""

def _big_query_settings(conn: psycopg.Connection) -> None:
    """For the scans over two days of reference traffic (inside a transaction): no parallel
    workers, whose sorts share the database container's 64 MB /dev/shm, and room to sort
    in memory instead of in temp files."""
    conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
    conn.execute("SET LOCAL work_mem = '512MB'")


def build(conn: psycopg.Connection, rules: list[Rule], cfg: dict) -> None:
    """cfg is config/scoring.yaml: the baseline uses the scorer's own thresholds."""
    accs: dict[tuple[str, str, str, str], Acc] = defaultdict(Acc)
    rule_fails: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    current: dict[str, list[str]] = defaultdict(list)
    for group, version in conn.execute("SELECT ref_group, version FROM ref_versions WHERE is_current"):
        current[group].append(version)
    with conn.transaction():
        _big_query_settings(conn)
        buckets = conn.execute(BUCKETS).fetchall()
    for app_id, group, bucket_type, bucket_branch in buckets:
        rows = conn.execute(SAMPLE, (app_id, bucket_type, bucket_branch, current.get(group, []),
                                     PER_BUCKET)).fetchall()
        for event_type, branch, source, message, envelope in rows:
            if not isinstance(message, dict):
                continue
            for finding in evaluate(rules, envelope):
                rule_fails[(event_type, branch, group)][finding.rule] += 1
            sub = subtype(event_type, message, source)
            accs[(event_type, sub, branch, group)].add(message)
            accs[(event_type, "*", branch, group)].add(message)

    now = datetime.now(timezone.utc)
    rows = []
    for (et, sub, branch, group), acc in accs.items():
        profile = acc.profile()
        if sub == "*":
            fails = rule_fails.get((et, branch, group), {})
            profile["rules"] = {rule: round(n / acc.n, 4) for rule, n in fails.items()}
        rows.append((et, sub, branch, group, acc.n, json.dumps(profile), now))
    per_type: dict[str, dict[str, dict]] = defaultdict(dict)
    thresholds = {"dup_hours": int(cfg.get("duplicate_window_hours", 24)),
                  "stale": int(cfg["stale_after_minutes"]), "future": int(cfg["future_after_minutes"])}
    with conn.transaction():
        _big_query_settings(conn)
        baseline_rows = conn.execute(BASELINE, thresholds).fetchall()
        burst_rows = conn.execute(BURSTS, (int(cfg["burst_per_second"]),)).fetchall()
    for group, et, total, dups, stale, future in baseline_rows:
        per_type[group][et] = {"messages": total, "duplicates": dups, "stales": stale,
                               "futures": future, "bursts": 0.0}
    for group, et, burst in burst_rows:
        if et in per_type.get(group, {}):
            per_type[group][et]["bursts"] = float(burst)

    def rates(c: dict) -> dict:
        n = c["messages"]
        return {"messages": n, "duplicate": c["duplicates"] / n, "stale": c["stales"] / n,
                "future": c["futures"] / n, "burst": c["bursts"] / n}

    baselines = {}
    for group, types in per_type.items():
        total = {k: sum(t[k] for t in types.values())
                 for k in ("messages", "duplicates", "stales", "futures", "bursts")}
        baselines[group] = {"all": rates(total),
                            "by_event_type": {et: rates(c) for et, c in types.items()}}

    # Closed-set values (paths whose distinct values stayed within MAX_DISTINCT) are
    # accumulated across runs, so rare values seen once by any reference app stay known.
    values: dict[tuple[str, str, str], int] = Counter()
    for (et, sub, branch, group), acc in accs.items():
        if sub != "*":
            continue
        for path, counter in acc.strs.items():
            for value, n in counter.items():
                values[(et, path, value[:200])] += n

    paths = {(et, branch, path) for (et, sub, branch, group), acc in accs.items() if sub == "*"
             for path in acc.present}

    with conn.transaction():
        with conn.cursor() as c:
            c.executemany(
                "INSERT INTO ref_paths (event_type, branch, path, last_seen) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (event_type, branch, path) DO UPDATE SET last_seen = EXCLUDED.last_seen",
                [(et, branch, path, now) for et, branch, path in sorted(paths)])
        conn.execute("DELETE FROM ref_paths WHERE last_seen < now() - interval '183 days'")
        with conn.cursor() as c:
            c.executemany(
                """
                INSERT INTO ref_values (event_type, path, value, count, last_seen)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (event_type, path, value) DO UPDATE SET
                    count = ref_values.count + EXCLUDED.count, last_seen = EXCLUDED.last_seen
                """, [(et, path, value, n, now) for (et, path, value), n in values.items()])
        conn.execute("DELETE FROM ref_values WHERE last_seen < now() - interval '183 days'")
        conn.execute("DELETE FROM ref_profiles")
        with conn.cursor() as c:
            c.executemany("INSERT INTO ref_profiles (event_type, subtype, branch, ref_group, n,"
                          " profile, built_at) VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)
        conn.execute("DELETE FROM ref_baselines")
        with conn.cursor() as c:
            c.executemany("INSERT INTO ref_baselines (ref_group, metrics, built_at)"
                          " VALUES (%s, %s, %s)",
                          [(g, json.dumps(m), now) for g, m in baselines.items()])
    log.info("profiles: %d buckets from %d reference messages; baselines for %s",
             len(rows), sum(a.n for k, a in accs.items() if k[1] == "*"), ", ".join(sorted(baselines)))
