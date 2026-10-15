"""Console report: documented-rule findings per application over recent traffic.

A development aid for tuning config/rules.yaml against real messages before
the scorer exists. Samples the newest messages per (app, event type).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from . import db
from .config import Settings
from .rules import evaluate, load_rules

SAMPLE_IDS = """
SELECT id, gw_ts FROM (
    SELECT m.id, m.gw_ts,
           row_number() OVER (PARTITION BY m.app_id, m.event_type ORDER BY m.gw_ts DESC) AS rn
      FROM messages m
     WHERE m.gw_ts > now() - make_interval(hours => %(hours)s)
) x WHERE rn <= %(per_bucket)s
"""


def rules_report(settings: Settings, hours: int = 24, per_bucket: int = 500,
                 app: str | None = None, min_messages: int = 50) -> None:
    rules = load_rules(settings.load_yaml("rules.yaml"))
    severity = {r.id: r.severity for r in rules}
    titles = {r.id: r.title for r in rules}
    checked: Counter[str] = Counter()
    fails: dict[str, Counter[str]] = defaultdict(Counter)
    example: dict[tuple[str, str], str] = {}
    groups: dict[str, str | None] = {}

    with db.connect(settings, application_name="rules-report") as conn:
        ids = conn.execute(SAMPLE_IDS, {"hours": hours, "per_bucket": per_bucket}).fetchall()
        with conn.transaction(), conn.cursor(name="report") as cur:   # server-side: rows stream in
            cur.itersize = 2000
            cur.execute(
                """
                SELECT a.software_name, a.ref_group, m.event_type, m.payload
                  FROM messages m JOIN apps a ON a.id = m.app_id
                 WHERE (m.id, m.gw_ts) IN (SELECT * FROM unnest(%s::bigint[], %s::timestamptz[]))
                """, ([i for i, _ in ids], [t for _, t in ids]))
            for name, group, event_type, payload in cur:
                if app and app.lower() not in name.lower():
                    continue
                groups[name] = group
                checked[name] += 1
                for f in evaluate(rules, payload):
                    fails[name][f.rule] += 1
                    example.setdefault((name, f.rule), f"{event_type}: {f.detail}")

    order = sorted(checked, key=lambda n: (groups[n] is None, -checked[n]))
    print(f"Documented-rule findings, newest {per_bucket} messages per app and event type, "
          f"last {hours} h\n")
    for name in order:
        if checked[name] < min_messages and not app:
            continue
        tag = f"  [reference: {groups[name]}]" if groups[name] else ""
        print(f"{name}{tag}: {checked[name]:,} messages checked")
        if not fails[name]:
            print("    no findings")
        for rule_id, n in sorted(fails[name].items(),
                                 key=lambda kv: (("error", "warning", "info").index(severity[kv[0]]),
                                                 -kv[1])):
            print(f"    {severity[rule_id]:<7} {rule_id:<9} {n:>6} {100 * n / checked[name]:5.1f}%  "
                  f"{example[(name, rule_id)][:90]}")
        print()
    skipped = [n for n in order if checked[n] < min_messages]
    if skipped and not app:
        print(f"({len(skipped)} apps with fewer than {min_messages} messages not shown)")
    print("\nRules: " + "; ".join(f"{k} {titles[k]}" for k in sorted(
        {r for c in fails.values() for r in c})))
