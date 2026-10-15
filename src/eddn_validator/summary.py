"""Plain-text rendering of a scorecard, for the `scorecard` command."""

from __future__ import annotations


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def print_card(card: dict) -> None:
    score = card["score"]
    window = "all messages on file" if card.get("all_messages") else f"last {card['range_days']} days"
    head = f"{card['app']} {card['version']} · {window} · {card['messages']:,} messages"
    print(head)
    print(f"Overall: {card['grade'] or '–'} ({score if score is not None else 'not scored'})")
    print("Game versions: " + ", ".join(f"{k} {v:,}" for k, v in card["gameversions"]["classes"].items()))
    print("Schemas: " + ", ".join(f"{k} {v:,}" for k, v in card["schemas"].items()))
    print()
    print(f"{'event type':<28} {'msgs':>7} {'score':>6}  " + "  ".join(f"{k[:6]:>6}" for k in
          ("rules", "struct", "values", "pairing", "behav")) + "  conf")
    for r in card["rows"]:
        layers = r.get("layers", {})
        cells = "  ".join(f"{'–' if layers.get(k) is None else round(layers[k]):>6}"
                          for k in ("rules", "structure", "values", "pairing", "behaviour"))
        label = r["event_type"] + (" (Legacy)" if r["branch"] == "legacy" else "")
        if not r["recognised"]:
            label += " ?"
        print(f"{label:<28} {r['messages']:>7,} {r.get('score', '–'):>6}  {cells}  {r['confidence']}")
    print()
    for r in card["rows"]:
        lines = []
        for rule in r.get("rules", []):
            if rule["severity"] != "info":
                lines.append(f"  rule {rule['severity']} {rule['id']} {_pct(rule['rate'])}: {rule['detail'][:100]}")
        f = r.get("fields", {})
        for m in f.get("missing_expected", [])[:5]:
            lines.append(f"  missing expected field {m['path']} ({m['subtype']}): sent in {_pct(m['app'])}")
        for u in f.get("unseen", [])[:5]:
            if u["severity"] != "info":
                lines.append(f"  field no reference app sends: {u['path']} ({u['messages']} messages)")
        for v in r.get("values", [])[:5]:
            if v["severity"] != "info":
                lines.append(f"  value {v['kind']} {v['path']} {_pct(v['rate'])}: {v['app']} vs {str(v['reference'])[:60]}")
        p = r.get("pairing") or {}
        for kind, k in (p.get("kinds") or {}).items():
            if k["mismatched_messages"]:
                lines.append(f"  pairing {kind}: {k['mismatched_messages']}/{k['messages']} disagree"
                             f" (fields: {', '.join(list(p.get('fields', {}))[:4])})")
        b = r.get("behaviour") or {}
        for k, v in (b.get("excess") or {}).items():
            if v > 0.005:
                lines.append(f"  behaviour {k}: {_pct(b[k])} vs reference {_pct(b['baseline'].get(k))}")
        if lines:
            print(r["event_type"] + (" (Legacy)" if r["branch"] == "legacy" else ""))
            print("\n".join(lines))
    if card["unrecognised"]:
        print("\nSchemas no reference app sends: " + ", ".join(card["unrecognised"]))
    if card["not_sent"]:
        print("Not sent (reference apps do): " + ", ".join(c["event_type"] for c in card["not_sent"]))
