"""Scorecards: one app version over each fixed range (docs/DESIGN.md §6.7, §6.8, §7).

For an app version the scorer
  * counts its messages per event type and range (all of them),
  * takes the newest `sample_per_event_type` messages per event type, and for
    each range scores the part of that sample inside the range on five layers:
      rules      documented EDDN rules (config/rules.yaml)
      structure  fields every reference app sends, per subtype
      values     types, known value sets and numeric ranges vs the reference apps
      pairing    same system/body/station/market vs reference facts (pair_results)
      behaviour  duplicates, late, future-dated and burst rates above the reference baseline
  * writes one scorecard per range to the scorecards table, which is all the web
    front end reads.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg

from .config import Settings
from .paths import Flat, flatten, string_shape, type_family
from .rules import Finding, Rule, evaluate, load_rules
from .subtypes import subtype

log = logging.getLogger(__name__)

# One scorecard per app version, over every message held for it. Retention keeps at most
# about six months, so a window this long simply means "everything still on file".
ALL_DAYS = 3650
RANGES = (ALL_DAYS,)
# The reference apps send over a million messages a day (EDMC 6.1.2 alone has tens of
# millions on file), so their own cards cover the last 7 days: counting duplicates across
# months of them hourly is not worth the load, and their history mainly serves as the reference.
REF_DAYS = 7
REF_RANGES = (REF_DAYS,)
LAYERS = ("rules", "structure", "values", "pairing", "behaviour")
HARD_PAIRS = ("system", "navroute", "body", "station")
MAX_EXAMPLES = 2
EXAMPLE_CHARS = 2500
MAX_FIELD_ROWS = 40
MAX_VALUE_ROWS = 25


def confidence_weight(messages: int, low_below: int) -> float:
    """An event type's weight in the overall score: its message count, scaled down in
    proportion below low_below messages (a smooth ramp, no jump at the threshold)."""
    return messages * min(1.0, messages / low_below)


def weak_spots(rows: list[dict], grades: dict) -> list[dict]:
    """Event types with enough data (confidence ok) that score below C. The overall score is
    weighted by message count, so one of these can sit under a good grade; the lists flag them."""
    spots = [{"event_type": r["event_type"], "branch": r["branch"], "score": r["score"],
              "grade": grade_for(r["score"], grades), "messages": r["messages"]}
             for r in rows if r.get("score") is not None and r["recognised"]
             and r["confidence"] == "ok" and r["score"] < grades["C"]]
    return sorted(spots, key=lambda s: s["score"])


def event_type_counts(rows: list[dict]) -> dict[str, int]:
    """Messages per event type on a card, branches added together."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["event_type"]] = counts.get(r["event_type"], 0) + r["messages"]
    return counts


def grade_for(score: float | None, grades: dict) -> str | None:
    if score is None:
        return None
    for letter in ("A", "B", "C", "D"):
        if score >= grades[letter]:
            return letter
    return "F"


def gameversion_class(gv: str | None) -> str:
    if gv is None:
        return "absent"
    if gv == "":
        return "empty"
    if gv.startswith("CAPI-Live-"):
        return "CAPI Live"
    if gv.startswith("CAPI-Legacy-"):
        return "CAPI Legacy"
    if gv.startswith("CAPI-"):
        return "CAPI (unsplit)"
    head = gv.split(".", 1)[0]
    if head.isdigit():
        return "Live 4.x" if int(head) >= 4 else "Legacy 3.x" if head == "3" else "other"
    return "other"


def example_json(envelope: dict) -> str:
    text = json.dumps(envelope, sort_keys=True)
    return text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS] + " …"


# ------------------------------------------------------------------ reference

class RefIndex:
    """Reference profiles, baselines and versions, loaded once per scorer run."""

    def __init__(self, conn: psycopg.Connection, cfg: dict):
        self.cfg = cfg
        self.min_n = int(cfg["reference_min_messages"])
        self.profiles: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
        self.counts: dict[tuple[str, str, str], dict[str, int]] = defaultdict(dict)
        for et, sub, branch, group, n, profile in conn.execute(
                "SELECT event_type, subtype, branch, ref_group, n, profile FROM ref_profiles"):
            self.profiles[(et, sub, branch)][group] = profile
            self.counts[(et, sub, branch)][group] = n
        self.event_types = {(et, b) for (et, sub, b) in self.profiles if sub == "*"}
        exclude = set(cfg.get("behaviour_baseline_exclude", []))
        self.baselines: dict[str, dict] = {
            group: metrics for group, metrics in conn.execute(
                "SELECT ref_group, metrics FROM ref_baselines") if group not in exclude}
        # Accumulated closed-set values: (event_type, path) -> {value: count}
        self.values: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        for et, path, value, count in conn.execute(
                "SELECT event_type, path, value, count FROM ref_values"):
            self.values[(et, path)][value] += count
        # Accumulated field paths: (event_type, branch) -> every path a reference app has sent.
        self.paths: dict[tuple[str, str], set[str]] = defaultdict(set)
        for et, branch, path in conn.execute("SELECT event_type, branch, path FROM ref_paths"):
            self.paths[(et, branch)].add(path)
        self.versions: dict[str, list[str]] = defaultdict(list)
        for group, version in conn.execute(
                "SELECT ref_group, version FROM ref_versions WHERE is_current ORDER BY published_at DESC"):
            self.versions[group].append(version)

    def baseline(self, event_type: str) -> dict[str, float]:
        """Worst reference rate per metric for this event type, from the reference apps that
        send enough of it. Overall rates are used only when none does: a group that never
        sends a type must not lend it the rates of its busiest types."""
        per_type = [m for metrics in self.baselines.values()
                    if (m := metrics.get("by_event_type", {}).get(event_type))
                    and m.get("messages", 0) >= self.min_n]
        sources = per_type or [metrics.get("all", {}) for metrics in self.baselines.values()]
        out: dict[str, float] = {}
        for m in sources:
            for k in ("duplicate", "stale", "future", "burst"):
                out[k] = max(out.get(k, 0.0), float(m.get(k, 0.0)))
        return out

    def known(self, event_type: str) -> bool:
        return any(et == event_type for et, _ in self.event_types)

    def branch_for(self, event_type: str, branch: str) -> str:
        if (event_type, branch) in self.event_types:
            return branch
        return "live"

    def bucket(self, event_type: str, sub: str, branch: str) -> tuple[str, dict[str, dict]]:
        """Profiles by group for a subtype, or the whole event type when the subtype is thin."""
        branch = self.branch_for(event_type, branch)
        groups = {g: p for g, p in self.profiles.get((event_type, sub, branch), {}).items()
                  if self.counts[(event_type, sub, branch)][g] >= self.min_n}
        if groups:
            return sub, groups
        groups = {g: p for g, p in self.profiles.get((event_type, "*", branch), {}).items()
                  if self.counts[(event_type, "*", branch)][g] >= self.min_n}
        return "*", groups

    def whole(self, event_type: str, branch: str) -> dict[str, dict]:
        return self.profiles.get((event_type, "*", self.branch_for(event_type, branch)), {})


# ------------------------------------------------------------------- sample

@dataclass
class Msg:
    gw_ts: datetime
    event_type: str
    branch: str
    envelope: dict
    flat: Flat
    sub: str
    findings: list[Finding]


@dataclass
class Row:
    event_type: str
    branch: str
    msgs: list[Msg] = field(default_factory=list)


# ------------------------------------------------------------------- layers

def score_rules(msgs: list[Msg], rules_by_id: dict[str, Rule], cfg: dict,
                ref_rates: dict[str, dict[str, float]]) -> tuple[float, list[dict]]:
    sev_w = cfg["severity_weights"]
    fails: Counter[str] = Counter()
    examples: dict[str, list] = defaultdict(list)
    details: dict[str, str] = {}
    for m in msgs:
        for f in {f.rule: f for f in m.findings}.values():
            fails[f.rule] += 1
            details.setdefault(f.rule, f.detail)
            if len(examples[f.rule]) < MAX_EXAMPLES:
                examples[f.rule].append(example_json(m.envelope))
    n = len(msgs)
    penalty = sum(sev_w[rules_by_id[r].severity] * c / n for r, c in fails.items())
    out = []
    for rule_id, count in sorted(fails.items(), key=lambda kv: (
            ("error", "warning", "info").index(rules_by_id[kv[0]].severity), -kv[1])):
        rule = rules_by_id[rule_id]
        out.append({
            "id": rule_id, "severity": rule.severity, "title": rule.title, "source": rule.source,
            "count": count, "rate": round(count / n, 4), "detail": details[rule_id],
            "reference": {g: rates.get(rule_id, 0.0) for g, rates in ref_rates.items()},
            "examples": examples[rule_id],
        })
    return 100 * max(0.0, 1 - penalty), out


def score_structure(row: Row, ref: RefIndex, cfg: dict) -> tuple[float | None, dict]:
    expected_t, common_t = cfg["expected_threshold"], cfg["common_threshold"]
    by_sub: dict[str, list[Msg]] = defaultdict(list)
    for m in row.msgs:
        by_sub[m.sub].append(m)
    whole = ref.whole(row.event_type, row.branch)
    known_paths = set().union(*(p["paths"].keys() for p in whole.values())) if whole else set()
    known_paths |= ref.paths.get((row.event_type, ref.branch_for(row.event_type, row.branch)), set())
    missing, unseen, differing, subtypes = [], Counter(), [], []
    weighted, weight = 0.0, 0
    unseen_msgs = 0
    journal = row.event_type.startswith("journal/1")
    for sub, msgs in sorted(by_sub.items(), key=lambda kv: -len(kv[1])):
        used, groups = ref.bucket(row.event_type, sub, row.branch)
        subtypes.append({"name": sub, "messages": len(msgs), "compared_with": used,
                         "reference": {g: ref.counts[(row.event_type, used,
                                                      ref.branch_for(row.event_type, row.branch))][g]
                                       for g in groups}})
        app_present: Counter[str] = Counter()
        for m in msgs:
            app_present.update(m.flat.types.keys())
            new = [p for p in m.flat.types if p not in known_paths]
            unseen.update(new)
            unseen_msgs += bool(new)
        n = len(msgs)
        if not groups:
            continue
        paths = set().union(*(g["paths"].keys() for g in groups.values()))
        expected = [p for p in paths
                    if all(g["paths"].get(p, {}).get("freq", 0) >= expected_t for g in groups.values())]
        miss = [(p, 1 - app_present[p] / n) for p in expected]
        sub_score = 100 * (1 - (sum(r for _, r in miss) / len(miss) if miss else 0.0))
        weighted += sub_score * n
        weight += n
        for p, rate in miss:
            if rate > 1 - expected_t:
                missing.append({"path": p, "subtype": sub, "app": round(1 - rate, 4),
                                "reference": {g: v["paths"][p]["freq"] for g, v in groups.items()}})
        # Rate differences only mean something against the same kind of message; against
        # the whole event type (thin subtype) every kind-specific field would differ.
        for p in sorted(paths | set(app_present)) if used != "*" else []:
            if p in expected:
                continue
            ref_freq = {g: v["paths"].get(p, {}).get("freq", 0.0) for g, v in groups.items()}
            app_freq = app_present[p] / n
            if max(ref_freq.values()) >= common_t or app_freq >= common_t:
                if abs(app_freq - max(ref_freq.values())) >= 0.25 and p in known_paths:
                    differing.append({"path": p, "subtype": sub, "app": round(app_freq, 4),
                                      "reference": ref_freq})
    if not weight:
        score = None
    else:
        score = weighted / weight
        if journal and unseen_msgs:
            # journal/1 accepts any key, so unknown fields are worth a warning there.
            score *= 1 - cfg["severity_weights"]["warning"] * unseen_msgs / len(row.msgs)
    return score, {
        "subtypes": subtypes,
        "missing_expected": sorted(missing, key=lambda x: x["app"])[:MAX_FIELD_ROWS],
        "unseen": [{"path": p, "messages": c, "severity": "warning" if journal else "info"}
                   for p, c in unseen.most_common(MAX_FIELD_ROWS)],
        "differing": differing[:MAX_FIELD_ROWS],
    }


def _merged_entry(groups: dict[str, dict], path: str) -> list[dict]:
    return [g["paths"][path] for g in groups.values() if path in g["paths"]]


def score_values(row: Row, ref: RefIndex, cfg: dict) -> tuple[float | None, list[dict], dict]:
    """(score, findings per field, summary). A finding row is one field; its count is the
    messages with that finding, and one message can appear in several rows, so the summary
    gives the distinct messages behind them."""
    vcfg = cfg["values"]
    open_fields = [re.compile(p) for p in vcfg.get("open_fields", [])]
    open_cache: dict[str, bool] = {}
    findings: dict[tuple[str, str], dict] = {}
    err_msgs = warn_msgs = checked = flagged_msgs = 0
    for m in row.msgs:
        _, groups = ref.bucket(row.event_type, m.sub, row.branch)
        if not groups:
            continue
        checked += 1
        has_err = has_warn = False
        seen_here: set[tuple[str, str]] = set()

        def note(key: tuple[str, str], template: dict, value: object = None) -> None:
            f = findings.setdefault(key, {**template, "count": 0, "app": []})
            if key not in seen_here:
                seen_here.add(key)
                f["count"] += 1
            if value is not None and value not in f["app"] and len(f["app"]) < 5:
                f["app"].append(value)

        for path, types in m.flat.types.items():
            entries = _merged_entry(groups, path)
            if not entries:
                continue
            ref_families = {type_family(t) for e in entries for t in e["types"]}
            bad = {type_family(t) for t in types} - ref_families
            if bad:
                has_err = True
                note((path, "type"), {"path": path, "kind": "type", "severity": "error",
                                      "reference": sorted(ref_families)}, sorted(bad))
                continue
            if path not in open_cache:
                open_cache[path] = any(p.search(path) for p in open_fields)
            if open_cache[path]:
                continue
            values = m.flat.scalars.get(path, [])
            strs = [e["str"] for e in entries if "str" in e]
            if strs and all(s["values"] is not None for s in strs):
                merged: Counter[str] = Counter()
                for s in strs:
                    merged.update(s["values"])
                history = ref.values.get((row.event_type, path))
                if history:
                    merged = merged | history
                samples = sum(merged.values())
                if samples >= vcfg["enum_min_samples"] and len(merged) <= vcfg["enum_max_distinct"]:
                    lower = {k.lower(): k for k in merged}
                    shapes = {string_shape(k) for k in merged}
                    for v in values:
                        if not isinstance(v, str) or v in merged:
                            continue
                        if v.lower() in lower:
                            kind, sev = "case", "info"
                        elif string_shape(v) not in shapes:
                            # A new value written in a form no reference app uses: typically a
                            # localised name, free text or a different encoding.
                            kind, sev = "unexpected_format", "warning"
                            has_warn = True
                        else:
                            kind, sev = "uncommon_value", "info"
                        note((path, kind), {"path": path, "kind": kind, "severity": sev,
                                            "reference": sorted(merged)[:12]}, v)
            nums = [e["num"] for e in entries if "num" in e]
            if nums and sum(n["samples"] for n in nums) >= vcfg["numeric_min_samples"]:
                lo, hi = min(n["min"] for n in nums), max(n["max"] for n in nums)
                span = (hi - lo) + 1e-9
                for v in values:
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    if lo - span * vcfg["numeric_margin"] <= v <= hi + span * vcfg["numeric_margin"]:
                        continue
                    wrong_sign = (lo >= 0 > v) or (hi <= 0 < v)
                    far = not (lo - span * vcfg["numeric_margin_warning"] <= v
                               <= hi + span * vcfg["numeric_margin_warning"])
                    # A field the reference apps only ever fill with fractions (0..1) holding
                    # a value above 2: a percentage, a units error the span test can't see.
                    fraction_scale = lo >= -1 and hi <= 1 and abs(v) > 2
                    sev = "warning" if (wrong_sign or far or fraction_scale) else "info"
                    has_warn = has_warn or sev == "warning"
                    kind = "range" if sev == "warning" else "beyond_reference"
                    note((path, kind), {"path": path, "kind": kind, "severity": sev,
                                        "reference": [lo, hi]}, v)
        flagged_msgs += bool(seen_here)
        err_msgs += has_err
        warn_msgs += has_warn and not has_err
    if not checked:
        return None, [], {}
    penalty = (err_msgs + cfg["severity_weights"]["warning"] * warn_msgs) / checked
    out = sorted(findings.values(), key=lambda f: (("error", "warning", "info").index(f["severity"]),
                                                    -f["count"]))
    for f in out:
        f["rate"] = round(f["count"] / checked, 4)
    summary = {"checked": checked, "flagged": flagged_msgs, "scored": err_msgs + warn_msgs}
    return 100 * max(0.0, 1 - penalty), out[:MAX_VALUE_ROWS], summary


def score_pairing(pairs: dict, cfg: dict) -> tuple[float | None, dict]:
    if not pairs or not pairs.get("kinds"):
        return None, pairs or {}
    hard_n = sum(k["messages"] for kind, k in pairs["kinds"].items() if kind in HARD_PAIRS)
    hard_bad = sum(k["mismatched_messages"] for kind, k in pairs["kinds"].items() if kind in HARD_PAIRS)
    soft = pairs["kinds"].get("market", {"messages": 0, "mismatched_messages": 0})
    total = hard_n + soft["messages"]
    if not total:
        return None, pairs
    penalty = (hard_bad + 0.5 * soft["mismatched_messages"]) / total
    return 100 * max(0.0, 1 - penalty), pairs


def score_behaviour(metrics: dict, ref: RefIndex, event_type: str) -> tuple[float | None, dict]:
    if not metrics or not metrics.get("messages"):
        return None, {}
    baseline = ref.baseline(event_type)
    excess = {k: max(0.0, metrics[k] - baseline.get(k, 0.0))
              for k in ("duplicate", "stale", "future", "burst")}
    penalty = min(1.0, sum(excess.values()))
    return 100 * (1 - penalty), {**metrics, "baseline": baseline, "excess": excess}


# The A7 covering index (indexes.py, on hold) would serve other apps' queries only with a
# matching "NOT is_big3" predicate. Without the index the predicate buys nothing, and it is not
# exact: is_big3 is stamped per row at ingest, so an app that once was a reference app keeps
# is_big3 rows after leaving reference_apps.yaml. Turn on together with the index.
OTHER_APP_INDEX = False


def _other(other: bool, alias: str = "") -> str:
    return f"AND NOT {alias}is_big3" if other and OTHER_APP_INDEX else ""


# ------------------------------------------------------------------- scorer

class Scorer:
    def __init__(self, conn: psycopg.Connection, settings: Settings):
        self.conn = conn
        self.cfg = settings.load_yaml("scoring.yaml")
        self.rules = load_rules(settings.load_yaml("rules.yaml"))
        self.rules_by_id = {r.id: r for r in self.rules}
        self.ref = RefIndex(conn, self.cfg)
        self.now = datetime.now(timezone.utc)

    # -- queries ---------------------------------------------------------
    def _counts_and_behaviour(self, app_id: int, version: str, ranges: tuple[int, ...],
                              other: bool = False) -> tuple[dict, dict]:
        """_counts and _behaviour in one pass over the version's messages (A6 in
        docs/research/db-performance-2026-10-14.md): grouped at the finer counts key, with the
        behaviour figures summed per event type here. Same results as the two separate queries."""
        c = self.cfg
        dup_hours = int(c.get("duplicate_window_hours", 24))
        with self.conn.transaction():
            # No parallel workers (their sorts share the database container's 64 MB /dev/shm and
            # fail with DiskFull on a big version), and room to sort in memory.
            self.conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            self.conn.execute("SET LOCAL work_mem = '256MB'")
            rows = self.conn.execute(
                """
                SELECT r.days, x.event_type, x.branch, x.is_test, x.game_version,
                       count(*), min(x.gw_ts),
                       count(*) FILTER (WHERE x.gap <= make_interval(hours => %s)),
                       count(*) FILTER (WHERE x.gw_ts - x.msg_ts > make_interval(mins => %s)),
                       count(*) FILTER (WHERE x.msg_ts > x.gw_ts + make_interval(mins => %s)),
                       count(*) FILTER (WHERE x.gap BETWEEN interval '25 seconds' AND interval '150 seconds')
                  FROM (SELECT event_type, branch, is_test, game_version, gw_ts, msg_ts,
                               gw_ts - lag(gw_ts) OVER (PARTITION BY content_hash ORDER BY gw_ts) AS gap
                          FROM messages
                         WHERE app_id = %s AND software_version = %s """ + _other(other) + """
                           -- reach back one duplicate window further, so the first messages in
                           -- the range can see their earlier copies (as profiles.BASELINE does);
                           -- the outer bound keeps everything counted inside the range
                           AND gw_ts > now() - make_interval(days => %s) - make_interval(hours => %s)) x,
                       unnest(%s::int[]) r(days)
                 WHERE x.gw_ts > now() - make_interval(days => r.days)
                 GROUP BY 1, 2, 3, 4, 5
                """, (dup_hours, c["stale_after_minutes"], c["future_after_minutes"], app_id, version,
                      max(ranges), dup_hours, list(ranges))).fetchall()
            burst = self._bursts(app_id, version, ranges, other)
        counts: dict = defaultdict(list)
        sums: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
        for days, et, branch, is_test, gv, n, oldest, dups, stale, future, resends in rows:
            counts[days].append((et, branch, is_test, gv, n, oldest))
            acc = sums[(days, et)]
            for i, v in enumerate((n, dups, stale, future, resends)):
                acc[i] += v
        behaviour: dict = defaultdict(dict)
        for (days, et), (n, dups, stale, future, resends) in sums.items():
            behaviour[days][et] = {"messages": n, "duplicate": dups / n, "stale": stale / n,
                                   "future": future / n, "burst": burst.get((days, et), 0.0) / n,
                                   "resend": resends / n}
        return counts, behaviour

    def _bursts(self, app_id: int, version: str, ranges: tuple[int, ...], other: bool) -> dict:
        rows = self.conn.execute(
            """
            SELECT r.days, b.event_type, sum(b.c) FROM (
                SELECT m.event_type, date_trunc('second', m.gw_ts) AS s, count(*) AS c,
                       max(m.gw_ts) AS t
                  FROM messages m
                 WHERE m.app_id = %s AND m.software_version = %s """ + _other(other, "m.") + """
                   AND m.gw_ts > now() - make_interval(days => %s)
                 GROUP BY 1, 2 HAVING count(*) >= %s
            ) b, unnest(%s::int[]) r(days)
            WHERE b.t > now() - make_interval(days => r.days)
            GROUP BY 1, 2
            """, (app_id, version, max(ranges), self.cfg["burst_per_second"], list(ranges))).fetchall()
        return {(d, et): float(n) for d, et, n in rows}

    def _sample(self, app_id: int, version: str, event_types: list[str],
                 max_days: int, branches: dict[str, set[str]] | None = None) -> list[Msg]:
        """Newest messages per event type, each read through messages_app_idx. When a branch
        the version sent (branches: event type -> {"live", "legacy"}) is absent from that
        sample, it gets its own sample, so a Legacy row is never left unscored."""
        with self.conn.transaction():
            # No parallel workers: a branch sample of a busy version scans a lot of rows, and a
            # parallel scan needs the database container's 64 MB /dev/shm (DiskFull otherwise).
            self.conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            rows_by_type = self._sample_rows(app_id, version, event_types, max_days, branches)
        msgs = []
        for rows in rows_by_type:
            for gw_ts, et, branch, source, envelope in rows:
                message = envelope.get("message") if isinstance(envelope, dict) else None
                if not isinstance(message, dict):
                    continue
                msgs.append(Msg(gw_ts, et, "legacy" if branch == "legacy" else "live", envelope,
                                flatten(message), subtype(et, message, source),
                                evaluate(self.rules, envelope)))
        return msgs

    def _sample_rows(self, app_id: int, version: str, event_types: list[str], max_days: int,
                     branches: dict[str, set[str]] | None) -> list[list]:
        per = int(self.cfg["sample_per_event_type"])
        out = []
        for event_type in event_types:
            rows = self.conn.execute(
                """
                SELECT gw_ts, event_type, branch, source, payload
                  FROM messages
                 WHERE app_id = %s AND software_version = %s AND event_type = %s
                   AND gw_ts > now() - make_interval(days => %s)
                 ORDER BY gw_ts DESC
                 LIMIT %s
                """, (app_id, version, event_type, max_days, per)).fetchall()
            sampled = {"legacy" if r[2] == "legacy" else "live" for r in rows}
            for missing in (branches or {}).get(event_type, set()) - sampled:
                rows += self.conn.execute(
                    """
                    SELECT gw_ts, event_type, branch, source, payload
                      FROM messages
                     WHERE app_id = %s AND software_version = %s AND event_type = %s
                       AND gw_ts > now() - make_interval(days => %s)
                       AND (branch = 'legacy') = %s
                     ORDER BY gw_ts DESC
                     LIMIT %s
                    """, (app_id, version, event_type, max_days, missing == "legacy", per)).fetchall()
            out.append(rows)
        return out

    def _pairs(self, app_id: int, version: str) -> dict:
        out: dict = defaultdict(lambda: defaultdict(lambda: {"kinds": {}, "fields": Counter(),
                                                             "examples": []}))
        for days, et, kind, n, bad, compared, mismatched in self.conn.execute(
                """
                SELECT r.days, p.event_type, p.pair_kind, count(*), count(*) FILTER (WHERE mismatched > 0),
                       sum(compared), sum(mismatched)
                  FROM pair_results p, unnest(%s::int[]) r(days)
                 WHERE p.app_id = %s AND p.software_version = %s
                   AND p.gw_ts > now() - make_interval(days => r.days)
                 GROUP BY 1, 2, 3
                """, (list(RANGES), app_id, version)):
            out[days][et]["kinds"][kind] = {"messages": n, "mismatched_messages": bad,
                                            "fields_compared": int(compared),
                                            "fields_mismatched": int(mismatched)}
        for days, et, key, n in self.conn.execute(
                """
                SELECT r.days, p.event_type, k, count(*)
                  FROM pair_results p, unnest(%s::int[]) r(days), jsonb_object_keys(p.mismatches) k
                 WHERE p.app_id = %s AND p.software_version = %s AND p.mismatches IS NOT NULL
                   AND p.gw_ts > now() - make_interval(days => r.days)
                 GROUP BY 1, 2, 3
                """, (list(RANGES), app_id, version)):
            out[days][et]["fields"][key] = n
        for et, kind, mismatches, gw_ts in self.conn.execute(
                """
                SELECT DISTINCT ON (event_type, pair_kind) event_type, pair_kind, mismatches, gw_ts
                  FROM pair_results
                 WHERE app_id = %s AND software_version = %s AND mismatches IS NOT NULL
                   AND gw_ts > now() - interval '180 days'
                 ORDER BY event_type, pair_kind, gw_ts DESC
                """, (app_id, version)):
            for days in RANGES:
                if gw_ts > self.now - timedelta(days=days):
                    out[days][et]["examples"].append({"kind": kind, "mismatches": mismatches})
        return out

    # -- card ------------------------------------------------------------
    def card(self, app: dict, version: str, days: int, counts: list, sample: list[Msg],
             behaviour: dict, pairs: dict) -> dict:
        cfg = self.cfg
        since = self.now - timedelta(days=days)
        conf = cfg["confidence"]
        rows: dict[tuple[str, str], Row] = {}
        for m in sample:
            if m.gw_ts > since:
                rows.setdefault((m.event_type, m.branch), Row(m.event_type, m.branch)).msgs.append(m)
        totals: Counter = Counter()
        gv_counts: Counter = Counter()
        gv_classes: Counter = Counter()
        branches: Counter = Counter()
        tests: Counter = Counter()
        oldest = min((row[5] for row in counts), default=None)
        for et, branch, is_test, gv, n, _ in counts:
            b = "legacy" if branch == "legacy" else "live"
            totals[(et, b)] += n
            gv_counts["(absent)" if gv is None else gv or "(empty)"] += n
            gv_classes[gameversion_class(gv)] += n
            branches[branch] += n
            tests["test" if is_test else "live"] += n
        for key in totals:
            rows.setdefault(key, Row(*key))

        weights = cfg["weights"]
        out_rows = []
        for (et, branch), row in sorted(rows.items(), key=lambda kv: -totals[kv[0]]):
            total = totals[(et, branch)]
            recognised = self.ref.known(et)
            confidence = ("insufficient" if total < conf["insufficient_below"]
                          else "low" if total < conf["low_below"] else "ok")
            entry: dict = {"event_type": et, "branch": branch, "messages": total,
                           "sampled": len(row.msgs), "confidence": confidence,
                           "recognised": recognised, "layers": {}}
            if row.msgs:
                whole = self.ref.whole(et, branch)
                ref_rates = {g: p.get("rules", {}) for g, p in whole.items()}
                layers: dict[str, float | None] = {}
                layers["rules"], entry["rules"] = score_rules(row.msgs, self.rules_by_id, cfg, ref_rates)
                layers["structure"], entry["fields"] = score_structure(row, self.ref, cfg)
                layers["values"], entry["values"], entry["values_summary"] = score_values(row, self.ref, cfg)
                is_ref_app = app["ref_group"] is not None
                if is_ref_app:
                    layers["pairing"], entry["pairing"] = None, {"note": "reference apps are not paired"}
                else:
                    p = pairs.get(et)
                    if p:
                        p = {"kinds": p["kinds"], "fields": dict(p["fields"].most_common(10)),
                             "examples": p["examples"][:3]}
                    layers["pairing"], entry["pairing"] = score_pairing(p, cfg)
                layers["behaviour"], entry["behaviour"] = score_behaviour(behaviour.get(et, {}), self.ref, et)
                available = {k: v for k, v in layers.items() if v is not None}
                if available and confidence != "insufficient":
                    w = sum(weights[k] for k in available)
                    entry["score"] = round(sum(weights[k] * v for k, v in available.items()) / w, 1)
                entry["layers"] = {k: (round(v, 1) if v is not None else None) for k, v in layers.items()}
            out_rows.append(entry)

        # Weighted by message count, ramped down below conf["low_below"] messages: the grade
        # says how much of what the app sends is sound. A weak low-volume event type barely
        # moves it, so those are listed separately (weak_spots).
        graded = [(r["score"], confidence_weight(r["messages"], conf["low_below"]))
                  for r in out_rows if r.get("score") is not None and r["recognised"]]
        score = (round(sum(s * w for s, w in graded) / sum(w for _, w in graded), 1)
                 if graded else None)
        sent = {r["event_type"] for r in out_rows}
        coverage = []
        for (et, sub, branch), groups in self.ref.profiles.items():
            if sub == "*" and branch == "live" and et not in sent:
                coverage.append({"event_type": et,
                                 "reference": {g: self.ref.counts[(et, sub, branch)][g] for g in groups}})
        return {
            "app": app["name"], "app_id": app["id"], "version": version, "range_days": days,
            "all_messages": days == ALL_DAYS,
            # The reference profiles describe the last 30 days: an older version's messages
            # may predate fields the game (and so the reference apps) added since.
            "older_than_reference": bool(sample) and max(m.gw_ts for m in sample) < self.now - timedelta(days=30),
            "oldest": oldest.isoformat() if oldest else None,
            "is_reference": app["ref_group"] is not None, "ref_group": app["ref_group"],
            "computed_at": self.now.isoformat(), "messages": sum(totals.values()),
            "score": score, "grade": grade_for(score, cfg["grades"]),
            "weak_spots": weak_spots(out_rows, cfg["grades"]),
            "weights": weights,
            "reference_versions": dict(self.ref.versions),
            "gameversions": {"classes": dict(gv_classes), "values": dict(gv_counts.most_common(10))},
            "branches": dict(branches), "schemas": dict(tests),
            "rows": out_rows,
            "unrecognised": sorted(r["event_type"] for r in out_rows if not r["recognised"]),
            "not_sent": sorted(coverage, key=lambda c: -sum(c["reference"].values())),
        }

    def score_version(self, app: dict, version: str) -> int:
        # A scorer can live for hours (followups' rebuild threads): stamp each card with its own time.
        self.now = datetime.now(timezone.utc)
        ranges = REF_RANGES if app["ref_group"] else RANGES
        other = app["ref_group"] is None
        counts, behaviour = self._counts_and_behaviour(app["id"], version, ranges, other)
        if not counts:
            self.conn.execute("DELETE FROM scorecards WHERE app_id = %s AND version = %s",
                              (app["id"], version))
            return 0
        pairs = {} if app["ref_group"] else self._pairs(app["id"], version)
        widest = max(d for d in ranges if counts.get(d))
        event_types = sorted({row[0] for row in counts[widest]})
        branches: dict[str, set[str]] = defaultdict(set)
        for row in counts[widest]:
            branches[row[0]].add("legacy" if row[1] == "legacy" else "live")
        sample = self._sample(app["id"], version, event_types, widest, branches)
        written = 0
        keep = [d for d in ranges if counts.get(d)]
        # Cards are built first, so the transaction that swaps them in lasts milliseconds.
        cards = [(days, self.card(app, version, days, counts[days], sample,
                                  behaviour.get(days, {}), pairs.get(days, {}))) for days in keep]
        with self.conn.transaction():
            # Also clears cards left from the earlier per-range scheme (1/7/14/30/90/180 days).
            self.conn.execute("DELETE FROM scorecards WHERE app_id = %s AND version = %s"
                              " AND range_days <> ALL(%s)", (app["id"], version, keep))
            for days, card in cards:
                self.conn.execute(
                    """
                    INSERT INTO scorecards (app_id, version, range_days, computed_at, messages,
                                            score, grade, card, weak_spots, event_types)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (app_id, version, range_days) DO UPDATE SET
                        computed_at = EXCLUDED.computed_at, messages = EXCLUDED.messages,
                        score = EXCLUDED.score, grade = EXCLUDED.grade, card = EXCLUDED.card,
                        weak_spots = EXCLUDED.weak_spots, event_types = EXCLUDED.event_types
                    """, (app["id"], version, days, self.now, card["messages"], card["score"],
                          card["grade"], json.dumps(card, default=str),
                          json.dumps(card["weak_spots"]),
                          json.dumps(event_type_counts(card["rows"]))))
                written += 1
        return written


def due_versions(conn: psycopg.Connection, refresh_hours: int,
                 min_minutes: int) -> list[tuple]:
    """App versions whose cards are missing, behind their newest message (and at least
    min_minutes old), or older than refresh_hours."""
    return conn.execute(
        """
        SELECT a.id, a.software_name, a.ref_group, v.version, max(s.computed_at) AS computed
          FROM app_versions v
          JOIN apps a ON a.id = v.app_id
          LEFT JOIN scorecards s ON s.app_id = v.app_id AND s.version = v.version
         WHERE v.last_seen > now() - interval '180 days'
           -- reference apps' cards cover 7 days: older versions have nothing to score
           AND (a.ref_group IS NULL OR v.last_seen > now() - interval '7 days')
         GROUP BY a.id, a.software_name, a.ref_group, v.version, v.last_seen
        HAVING max(s.computed_at) IS NULL
            OR (max(s.computed_at) < v.last_seen
                AND max(s.computed_at) < now() - make_interval(mins => %s))
            OR max(s.computed_at) < now() - make_interval(hours => %s)
         ORDER BY max(s.computed_at) NULLS FIRST, v.last_seen DESC
        """, (min_minutes, refresh_hours)).fetchall()


def run(conn: psycopg.Connection, settings: Settings, max_seconds: float = 600) -> None:
    started = time.monotonic()
    conn.execute("DELETE FROM scorecards s WHERE NOT EXISTS (SELECT 1 FROM app_versions v"
                 " WHERE v.app_id = s.app_id AND v.version = s.version"
                 " AND v.last_seen > now() - interval '180 days')")
    # Reference-app cards cover the last REF_DAYS: a version that has gone quiet has nothing
    # left to show, and due_versions no longer visits it.
    conn.execute("DELETE FROM scorecards s USING apps a, app_versions v"
                 " WHERE a.id = s.app_id AND a.ref_group IS NOT NULL"
                 " AND v.app_id = s.app_id AND v.version = s.version"
                 " AND v.last_seen < now() - make_interval(days => %s)", (REF_DAYS,))
    scorer = Scorer(conn, settings)
    due = due_versions(conn, int(scorer.cfg["refresh_hours"]),
                       int(scorer.cfg.get("min_recompute_minutes", 60)))
    done = cards = 0
    for app_id, name, group, version, _ in due:
        if time.monotonic() - started > max_seconds:
            break
        try:
            cards += scorer.score_version({"id": app_id, "name": name, "ref_group": group}, version)
        except Exception:
            log.exception("scoring %s %s failed", name, version)
        done += 1
    log.info("scorecards: %d of %d due app versions scored (%d cards) in %.0fs",
             done, len(due), cards, time.monotonic() - started)


def score_one(conn: psycopg.Connection, settings: Settings, app_name: str, version: str) -> dict:
    row = conn.execute("SELECT id, software_name, ref_group FROM apps WHERE software_name = %s",
                       (app_name,)).fetchone()
    if row is None:
        raise SystemExit(f"no app named {app_name!r}")
    scorer = Scorer(conn, settings)
    scorer.score_version({"id": row[0], "name": row[1], "ref_group": row[2]}, version)
    cards = conn.execute("SELECT range_days, card FROM scorecards WHERE app_id = %s AND version = %s",
                         (row[0], version)).fetchall()
    return {d: c for d, c in cards}
