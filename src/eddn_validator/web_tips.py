"""Hover text for the scorecard page: what each field means, plus a breakdown from the card."""

from __future__ import annotations

from collections import Counter

LAYER_NAMES = {"rules": "Rules", "structure": "Fields", "values": "Values",
               "pairing": "Pairing", "behaviour": "Behaviour"}
PAIR_NAMES = {"system": "System name and position", "navroute": "Route systems",
              "body": "Body data (fixed details)", "station": "Station name, type and system",
              "market": "Market within 5 minutes (counts half)"}
VALUE_KINDS = {"type": "wrong type", "unexpected_format": "unfamiliar format",
               "uncommon_value": "value not seen from reference apps", "case": "different capitals",
               "range": "far outside reference range", "beyond_reference": "outside reference range"}
BEHAVIOUR = (("duplicate", "Identical message sent again"), ("stale", "Sent over an hour late"),
             ("future", "Timestamp in the future"), ("burst", "In bursts of 20+ a second"))

STATIC = {
    "grade": "Overall grade from the score: A 90+, B 80+, C 70+, D 60+, F below 60.",
    "window": "Every message from this version still on file. Messages are kept for about six "
              "months, so a long-lived version is scored on its most recent six months.",
    "window_ref": "Reference apps send millions of messages, so their own cards cover the last "
                  "7 days.",
    "computed": "When this card was last worked out. Cards refresh within the hour after new "
                "messages arrive, and at least every 6 hours.",
    "versions": "Every version of this app seen on EDDN, most recently active first, each with "
                "the grade from its own scorecard. Versions with no messages left on file (older "
                "than about six months) have no scorecard.",
    "versions_messages": "Messages from this version since it was first seen, before any were "
                         "removed by the 6-month retention.",
    "tile_messages": "Every message from this version on the card, all event types.",
    "tile_sent": "Kinds of event (schema, and journal event for journal/1) this version sent.",
    "tile_scored": "Kinds of event with enough messages (5 or more) to be scored.",
    "tile_not_sent": "Kinds of event the reference apps send but this version did not. Listed, never counted against the grade.",
    "gameversions": "Which game the data says it came from. Consumers of EDDN often filter on "
                    "this.\nLive 4.x: the current game.\nLegacy 3.x: the old Horizons 3.8 galaxy.\n"
                    "CAPI Live/Legacy: data from Frontier's online service.\n"
                    "absent/empty: the version was not given (a rule failure).",
    "schemas": "live: the normal EDDN schemas. test: the /test schemas, for testing an app.",
    "col_event": "The kind of message: an EDDN schema, plus the journal event for journal/1. "
                 "Click a row for the findings behind its score.",
    "col_messages": "Messages of this kind from this version.",
    "col_score": "Score out of 100: the weighted average of the five layers that had data.",
    "col_rules": "EDDN's written rules (30% of the score). Errors count fully, warnings half.",
    "col_structure": "Fields compared with the reference apps' habits for this kind of message: "
                     "missing ones they all send, extra ones none send (20%).",
    "col_values": "Value types, known values and ranges compared with the reference apps' overall "
                  "profile, not specific messages (20%). Notes are shown but not scored.",
    "col_pairing": "Each message compared with the reference record for the same system, body, "
                   "station or market (20%).",
    "col_behaviour": "Duplicates, late or future-dated messages and bursts, compared with the "
                     "reference apps' own rates (10%).",
    "col_confidence": "ok: 30 or more messages. low: 5 to 29, counts half in the grade. "
                      "too few: under 5, shown but not scored.",
    "rule": "The rule's ID, what it requires, and where EDDN's documentation says so.",
    "severity": "serious counts fully against the Rules score, minor counts half, a note is "
                "shown but never counted.",
    "rule_messages": "Messages that broke this rule, and their share of the messages checked.",
    "rule_detail": "What was wrong in the first such message. Open an example for the full message.",
    "rule_reference": "How often each reference app breaks the same rule, for comparison.",
    "field_kind": "expected, often missing: every reference app sends it, this app often does "
                  "not.\nno reference app sends it: an extra field.\nsent at a different rate: "
                  "an optional field sent much more or less often than the reference apps.",
    "field_app": "Share of this app's messages (of that kind) that include the field.",
    "field_reference": "Share of each reference app's messages that include the field.",
    "value_finding": "wrong type: e.g. text where a number belongs (error).\nunfamiliar format: "
                     "written in a form no reference app uses, such as a display name (warning).\n"
                     "far outside the reference range: a units or sign error (warning).\nOther "
                     "kinds are notes, shown but not scored: \"outside the reference range\" means "
                     "beyond what the reference apps happened to send recently, not impossible.",
    "pair_compared": "What was compared with the reference record of the same object.",
    "value_messages": "Messages with this finding in this field, and their share of the messages "
                      "checked. One message can appear in several rows (one unusual body can be "
                      "outside the range in mass, radius and gravity at once).",
    "pair_messages": "Messages that could be matched with a reference app's report.",
    "pair_disagree": "Matched messages where at least one compared field differed.",
    "pair_fields": "Individual fields compared across all matched messages.",
    "behaviour_measure": "Only the amount above the worst reference app's rate for this kind "
                         "of event counts against the score.",
    "behaviour_app": "Share of this app's messages with this behaviour.",
    "behaviour_reference": "The highest rate among the reference apps (EDO Materials Helper "
                           "excluded) for this kind of event.",
    "not_sent_reference": "Reference apps that send this kind of event, with how many of their "
                          "messages are on file.",
}


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _row_tips(row: dict, card: dict) -> dict[str, str]:
    tips: dict[str, str] = {}
    days = card["range_days"]
    window = "on file" if card.get("all_messages") else f"in the last {days} day{'s' if days > 1 else ''}"
    tips["messages"] = (f"{row['messages']:,} messages of this kind {window}; the newest "
                        f"{row['sampled']:,} were checked in detail.")
    layers = row.get("layers") or {}
    weights = card.get("weights", {})
    if row.get("score") is not None:
        parts = [f"{LAYER_NAMES[k]} {round(v)} × {weights.get(k, 0)}%"
                 for k, v in layers.items() if v is not None]
        tips["score"] = ("Weighted average of the layers with data:\n" + "\n".join(parts)
                         + f"\n= {row['score']}")
    else:
        tips["score"] = ("Not scored: fewer than 5 messages." if row["confidence"] == "insufficient"
                         else "Not scored: no layer had data.")

    rules = [r for r in row.get("rules") or [] if r["severity"] != "info"]
    notes = [r for r in row.get("rules") or [] if r["severity"] == "info"]
    if rules:
        tips["rules"] = "Rules broken (share of messages):\n" + "\n".join(
            f"{r['id']} {r['severity']} {_pct(r['rate'])}" for r in rules[:8])
    else:
        tips["rules"] = "No documented EDDN rule broken."
    if notes:
        tips["rules"] += f"\nPlus {len(notes)} note{'s' if len(notes) > 1 else ''} (not scored)."

    fields = row.get("fields") or {}
    if layers.get("structure") is None:
        tips["structure"] = "No reference app data for this kind of message."
    else:
        subs = fields.get("subtypes") or []
        lines = [f"{s['name']}: {s['messages']:,} messages" +
                 (" (vs whole event type)" if s.get("compared_with") == "*" else "") for s in subs[:5]]
        tips["structure"] = ("Compared by kind:\n" + "\n".join(lines) +
                             f"\nExpected fields often missing: {len(fields.get('missing_expected') or [])}"
                             f"\nFields no reference app sends: {len(fields.get('unseen') or [])}")

    values = row.get("values") or []
    if layers.get("values") is None:
        tips["values"] = "No reference app data to compare values with."
    elif values:
        counts = Counter(VALUE_KINDS.get(v["kind"], v["kind"]) for v in values)
        tips["values"] = "Value findings (fields affected):\n" + "\n".join(
            f"{k}: {n}" for k, n in counts.items())
    else:
        tips["values"] = "Every value matches what the reference apps send."

    pairing = row.get("pairing") or {}
    if pairing.get("note"):
        tips["pairing"] = "Reference apps are not paired against themselves."
    elif pairing.get("kinds"):
        tips["pairing"] = "Same object reported by a reference app:\n" + "\n".join(
            f"{PAIR_NAMES.get(k, k)}: {v['messages']:,} matched, {v['mismatched_messages']:,} disagree"
            for k, v in sorted(pairing["kinds"].items()))
    else:
        tips["pairing"] = "No message could be matched with a reference app's report of the same object."

    b = row.get("behaviour") or {}
    if b.get("messages"):
        base = b.get("baseline") or {}
        lines = [f"{label}: {_pct(b.get(k))} (reference {_pct(base.get(k))})" for k, label in BEHAVIOUR]
        if "resend" in b:
            lines.append(f"Re-sent 25–150 s later: {_pct(b['resend'])} (noted, not scored)")
        tips["behaviour"] = "\n".join(lines)
    else:
        tips["behaviour"] = "No behaviour data."

    tips["confidence"] = {"ok": f"{row['messages']:,} messages: enough for a full-weight score.",
                          "low": f"{row['messages']:,} messages: scored, but counts half in the grade.",
                          "insufficient": f"{row['messages']:,} messages: too few to score."}[row["confidence"]]
    tips["event"] = STATIC["col_event"] + (
        "\nNot sent by any reference app: checked against the rules only, left out of the grade."
        if not row.get("recognised") else "")
    return tips


def annotate(card: dict) -> dict:
    """Add hover text to a card (in place) and return it."""
    scored = [r for r in card["rows"] if r.get("score") is not None]
    card["tip_grade"] = (f"Score {card['score']} out of 100. " if card.get("score") is not None else "") + \
        STATIC["grade"] + (f"\nAverage of {len(scored)} scored event types, weighted by messages."
                           if scored else "")
    for row in card["rows"]:
        row["tips"] = _row_tips(row, card)
    return card


GAMEVERSION_TIPS = {
    "Live 4.x": "Live 4.x: data from the current game",
    "Legacy 3.x": "Legacy 3.x: data from the old Horizons 3.8 galaxy, which many services ignore",
    "CAPI Live": "CAPI Live: data from Frontier's online service for the current game",
    "CAPI Legacy": "CAPI Legacy: data from Frontier's online service for the Legacy galaxy",
    "CAPI (unsplit)": "CAPI (unsplit): online-service data labelled the old way, without Live or Legacy",
    "absent": "absent: the game version was not sent at all (rule GEN-001)",
    "empty": "empty: the game version was sent blank",
    "other": "other: a game version in an unrecognised form",
}
SEVERITY_TIPS = {
    "error": "serious: breaks a MUST in EDDN's documentation and is reliably detected. Counts fully.",
    "warning": "minor: breaks a SHOULD, or a MUST detected by a heuristic. Counts half.",
    "info": "note: advice or a game-data oddity. Shown, never counted.",
}
VALUE_TIPS = {
    "type": "A value of the wrong type, such as text where every reference app sends a number. Counts as an error.",
    "unexpected_format": "A value written in a form no reference app uses, such as a translated display name "
                         "instead of the game's internal one. Counts as a warning.",
    "uncommon_value": "A value the reference apps have not sent, but in a familiar form (a rare star type, "
                      "a new faction state). Noted only.",
    "case": "The same value as the reference apps, with different capital letters. Noted only.",
    "range": "A number far outside anything the reference apps send (over 100 times their spread): a "
             "units or sign error. Counts as a warning.",
    "beyond_reference": "A number somewhat outside the reference apps' range. Noted only.",
}
PAIR_TIPS = {
    "system": "The system's name and position compared with a reference app's report of the same "
              "SystemAddress.",
    "navroute": "Each route hop's system name and position compared with reference reports.",
    "body": "The body's fixed details (mass, radius, gravity, atmosphere, materials, rings, orbit) "
            "compared with a reference app's scan of the same body.",
    "station": "The station's name and type (if seen within 7 days) and the system it is in "
               "(carriers, megaships and Stronghold Carriers excepted), compared with reference reports of the same MarketID.",
    "market": "Goods and buy/sell prices compared with a reference app's snapshot of the same market "
              "within 5 minutes. Counts half: prices can change between visits.",
}
BEHAVIOUR_TIPS = {
    "duplicate": "Messages identical to one already sent by this version (same content and timestamp).",
    "stale": "Messages that reached EDDN more than an hour after the event happened.",
    "future": "Messages timestamped more than 5 minutes after EDDN received them; often local "
              "time labelled as universal time.",
    "burst": "Messages sent in bursts of 20 or more of one kind in a single second; usually a "
             "journal being replayed.",
}
