"""Documented EDDN rules, checked one message at a time (layer 1, docs/DESIGN.md §6.1).

Each rule's metadata (severity, schemas it applies to, title, citation, on/off)
lives in config/rules.yaml; each check is a function registered here under the
same ID. A check returns None when the message passes, or a short detail
string saying what failed. The rule IDs and sources are those of
docs/research/eddn-schemas.md §23.

Only rules that can be judged from a relayed message are here. Pre-gateway
rules (transport, uploaderID content) cannot be seen on the relay, and the
SystemAddress/StarPos cross-checks (GEN-030, GEN-031) need reference data, so
they belong to later phases.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta

from .classify import game_branch, parse_schema_ref, parse_ts
from .facts import valid_pos
from .id64 import decode as decode_id64
from .id64 import is_procedural, name_matches_address

log = logging.getLogger(__name__)

SEVERITIES = ("error", "warning", "info")
MISSING = object()

Check = Callable[["Ctx"], "str | None"]
REGISTRY: dict[str, Check] = {}


def check(rule_id: str) -> Callable[[Check], Check]:
    def register(func: Check) -> Check:
        if rule_id in REGISTRY:
            raise ValueError(f"duplicate check {rule_id}")
        REGISTRY[rule_id] = func
        return func
    return register


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    schemas: frozenset[str] | None   # None: every schema
    title: str
    source: str
    check: Check

    def applies_to(self, schema_name: str) -> bool:
        return self.schemas is None or schema_name in self.schemas


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    detail: str


class Ctx:
    """One relayed envelope, with the pieces checks need."""

    def __init__(self, envelope: dict):
        self.envelope = envelope
        header = envelope.get("header")
        self.header = header if isinstance(header, dict) else {}
        message = envelope.get("message")
        self.message = message if isinstance(message, dict) else {}
        _, self.schema_name, self.is_test = parse_schema_ref(envelope.get("$schemaRef"))
        event = self.message.get("event")
        self.event = event if isinstance(event, str) else None
        self.gameversion = self.header.get("gameversion", MISSING)
        self.gamebuild = self.header.get("gamebuild", MISSING)
        self.gv = self.gameversion if isinstance(self.gameversion, str) else ""
        self.msg_ts = parse_ts(self.message.get("timestamp"))
        self.gw_ts = parse_ts(self.header.get("gatewayTimestamp"))

    @property
    def system_name(self) -> object:
        m = self.message
        return m.get("StarSystem", m.get("System", m.get("SystemName", m.get("systemName"))))


# --------------------------------------------------------------------- engine

def load_rules(config: dict) -> list[Rule]:
    """Combine config/rules.yaml with the registered checks.

    Every rule in the YAML needs a check and vice versa, so neither can drift.
    Disabled rules are dropped.
    """
    entries: dict[str, dict] = config.get("rules", {})
    missing_checks = sorted(set(entries) - set(REGISTRY))
    missing_config = sorted(set(REGISTRY) - set(entries))
    if missing_checks or missing_config:
        raise ValueError(f"rules.yaml and rules.py disagree: no check for {missing_checks}, "
                         f"no config for {missing_config}")
    rules = []
    for rule_id, entry in entries.items():
        severity = entry.get("severity")
        if severity not in SEVERITIES:
            raise ValueError(f"{rule_id}: severity must be one of {SEVERITIES}")
        if not entry.get("enabled", True):
            continue
        schemas = entry.get("schemas", "*")
        rules.append(Rule(
            id=rule_id,
            severity=severity,
            schemas=None if schemas == "*" else frozenset(schemas),
            title=entry["title"],
            source=entry.get("source", ""),
            check=REGISTRY[rule_id],
        ))
    return rules


_failed_checks: set[str] = set()


def evaluate(rules: list[Rule], envelope: dict) -> list[Finding]:
    """Findings for one envelope. A check that raises on hostile input is skipped for that
    message (logged once per rule), so one message can never abort a scoring or profile run."""
    ctx = Ctx(envelope)
    findings = []
    for rule in rules:
        if not rule.applies_to(ctx.schema_name):
            continue
        try:
            detail = rule.check(ctx)
        except Exception:
            if rule.id not in _failed_checks:
                _failed_checks.add(rule.id)
                log.exception("rule %s raised on a message; skipping it for such messages", rule.id)
            continue
        if detail is not None:
            findings.append(Finding(rule.id, rule.severity, detail))
    return findings


# -------------------------------------------------------------------- helpers

def walk(value: object, path: str = "") -> Iterator[tuple[str, str, object]]:
    """Every (path, key, value) in nested dicts and lists."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, str(key), child
            yield from walk(child, child_path)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, f"{path}[{i}]")


def first_key(value: object, predicate: Callable[[str], bool]) -> str | None:
    for path, key, _ in walk(value):
        if predicate(key):
            return path
    return None


JOURNAL_GV = re.compile(r"^\d+(\.\d+)+")
JOURNAL_BUILD = re.compile(r"^r\d+/r\d+\s*$")
CAPI_GV = re.compile(r"^CAPI-(?:(Live|Legacy)-)?(market|shipyard|journal)$")
CAPI_ENDPOINT = {
    "commodity/3": "market",
    "fcmaterials_capi/1": "market",
    "shipyard/2": "shipyard",
    "outfitting/2": "shipyard",
    "outfitting/3": "shipyard",
}
VERSION_LIKE = re.compile(r"^v?\d+(\.\d+){1,3}([-+._ ]?[0-9A-Za-z.+-]*)?$")
BETA = re.compile(r"alpha|beta", re.IGNORECASE)
# Fleet carriers: XXX-XXX. Squadron carriers: four characters (e.g. BIGG, R312).
CARRIER_ID = re.compile(r"^(?:[A-Z0-9]{3}-[A-Z0-9]{3}|[A-Z0-9]{4})$")
PERSONAL_KEYS = frozenset({
    "FID", "Commander", "Credits", "Loan", "ShipID", "ShipName", "ShipIdent", "FuelCapacity",
    "Reputation", "MyReputation", "Wanted", "ActiveFine", "Bounty",
})
# Beta client builds (gameversion, gamebuild without trailing space) that Frontier
# no longer marks as beta in either field. 4.4.0.0 r330116/r0 is deliberately
# absent: Frontier reused that beta build id for the live release.
# Source: EliteDangerousCore EliteReleaseDates.IsBeta (docs/research/edcd-github-updates.md).
BETA_BUILDS = frozenset({
    ("4.4.1.0", "r332668/r0"), ("4.4.0.0", "r329880/r0"), ("4.3.3.0", "r327080/r0"),
    ("4.3.1.0", "r324270/r0"), ("4.3.0.0", "r321601/r0"), ("4.2.1.0", "r319022/r0"),
    ("4.1.3.0", "r316037/r0"), ("4.0.0.1903", "r308286/r0"),
})
NO_MARKET = 2**64 - 1   # Frontier's "no market" MarketID sentinel
UNIQUE_ARRAYS = {   # arrays the schemas mark uniqueItems
    "commodity/3": ("prohibited",),
    "outfitting/2": ("modules",),
    "outfitting/3": ("modules",),
    "shipyard/2": ("ships",),
}
ORBITAL_ELEMENTS = ("SemiMajorAxis", "Eccentricity", "OrbitalInclination", "Periapsis",
                    "OrbitalPeriod", "AscendingNode", "MeanAnomaly")


def _items(value: object) -> list:
    return value if isinstance(value, list) else []


def _is_utc(ts: object) -> bool:
    return isinstance(ts, str) and (ts.endswith(("Z", "z")) or ts.endswith(("+00:00", "+0000")))


# ------------------------------------------------------- header / gameversion

@check("GEN-001")
def gameversion_missing(ctx: Ctx) -> str | None:
    return "header.gameversion absent" if ctx.gameversion is MISSING else None


@check("GEN-002")
def gamebuild_missing(ctx: Ctx) -> str | None:
    return "header.gamebuild absent" if ctx.gamebuild is MISSING else None


@check("GEN-003")
def gameversion_unknown_form(ctx: Ctx) -> str | None:
    gv = ctx.gameversion
    if gv is MISSING or gv == "":
        return None
    if not isinstance(gv, str):
        return f"gameversion is a {type(gv).__name__}"
    if JOURNAL_GV.match(gv) or CAPI_GV.match(gv):
        return None
    return f"gameversion {gv!r} is neither a game version nor a CAPI-* value"


@check("GEN-003B")
def gameversion_unsplit_capi(ctx: Ctx) -> str | None:
    m = CAPI_GV.match(ctx.gv)
    if m and m.group(1) is None:
        return f"gameversion {ctx.gv!r} lacks Live/Legacy (CAPI-Live-*/CAPI-Legacy-*)"
    return None


@check("GEN-004")
def capi_endpoint_mismatch(ctx: Ctx) -> str | None:
    m = CAPI_GV.match(ctx.gv)
    if not m or ctx.schema_name == "fcmaterials_journal/1":   # FCJ-001 covers that schema
        return None
    expected = CAPI_ENDPOINT.get(ctx.schema_name, "journal")
    if m.group(2) != expected:
        return f"gameversion {ctx.gv!r} on {ctx.schema_name}; expected CAPI-*-{expected}"
    return None


@check("GEN-005")
def gamebuild_mirrors_journal(ctx: Ctx) -> str | None:
    gb = ctx.gamebuild
    if isinstance(gb, str) and gb.startswith("CAPI-") and (
            not ctx.gv.startswith("CAPI-") or gb != ctx.gv):
        return f"gamebuild {gb!r} with gameversion {ctx.gv!r}"
    # Mirroring gameversion is allowed only for CAPI values (Dev:279-280), not a journal version.
    if isinstance(gb, str) and gb and gb == ctx.gv and JOURNAL_GV.match(ctx.gv):
        return f"gamebuild {gb!r} copies the journal gameversion"
    return None


@check("GEN-006")
def gamebuild_stripped(ctx: Ctx) -> str | None:
    gb = ctx.gamebuild
    if isinstance(gb, str) and re.fullmatch(r"r\d+/r\d+", gb):
        return f"gamebuild {gb!r} has no trailing space (journal values usually do)"
    return None


@check("GEN-007")
def gameversion_empty(ctx: Ctx) -> str | None:
    return "gameversion is empty" if ctx.gameversion == "" else None


@check("GEN-008")
def capi_schema_with_journal_version(ctx: Ctx) -> str | None:
    if JOURNAL_GV.match(ctx.gv):
        return f"journal gameversion {ctx.gv!r} on a CAPI-only schema"
    return None


@check("GEN-009")
def journal_version_without_build(ctx: Ctx) -> str | None:
    # Fileheader and LoadGame carry `build` beside `gameversion` in every client that writes one
    # (Dev:291-293), so a journal value with the other left empty means the sender dropped it.
    gb = ctx.gamebuild
    if not isinstance(gb, str):
        return None
    if JOURNAL_GV.match(ctx.gv) and not gb.strip():
        return f"journal gameversion {ctx.gv!r} with an empty gamebuild"
    if ctx.gv == "" and JOURNAL_BUILD.match(gb):
        return f"journal gamebuild {gb!r} with an empty gameversion"
    return None


@check("GEN-009B")
def capi_with_journal_build(ctx: Ctx) -> str | None:
    # CAPI-*-journal is excluded: there the journal's build is the legitimate source value.
    m = CAPI_GV.match(ctx.gv)
    gb = ctx.gamebuild
    if m and m.group(2) in ("market", "shipyard") and isinstance(gb, str) and JOURNAL_BUILD.match(gb):
        return f"journal build {gb!r} sent as gamebuild with {ctx.gv!r}"
    return None


@check("GEN-010")
def software_name_empty(ctx: Ctx) -> str | None:
    name = ctx.header.get("softwareName")
    return "softwareName is empty" if not isinstance(name, str) or not name.strip() else None


@check("GEN-011")
def software_version_empty(ctx: Ctx) -> str | None:
    v = ctx.header.get("softwareVersion")
    return "softwareVersion is empty" if not isinstance(v, str) or not v.strip() else None


@check("GEN-011B")
def software_version_not_versionlike(ctx: Ctx) -> str | None:
    v = ctx.header.get("softwareVersion")
    if isinstance(v, str) and v.strip() and not VERSION_LIKE.match(v.strip()):
        return f"softwareVersion {v!r} does not look like a version number"
    return None


@check("GEN-014")
def beta_on_live_schema(ctx: Ctx) -> str | None:
    if ctx.is_test:
        return None
    gb = ctx.gamebuild if isinstance(ctx.gamebuild, str) else ""
    if BETA.search(ctx.gv) or BETA.search(gb):
        return f"beta/alpha game ({ctx.gv!r} {gb!r}) on a live schema"
    return None


@check("GEN-014B")
def known_beta_build_on_live_schema(ctx: Ctx) -> str | None:
    if ctx.is_test:
        return None
    gb = ctx.gamebuild.strip() if isinstance(ctx.gamebuild, str) else ""
    if (ctx.gv.strip(), gb) in BETA_BUILDS:
        return f"beta client {ctx.gv} {gb} on a live schema"
    return None


# ------------------------------------------------------------ message-wide

@check("GEN-020")
def localised_keys(ctx: Ctx) -> str | None:
    path = first_key(ctx.message, lambda k: k.endswith("_Localised"))
    return f"{path} present" if path else None


@check("GEN-021")
def timestamp_not_utc(ctx: Ctx) -> str | None:
    ts = ctx.message.get("timestamp")
    if isinstance(ts, str) and not _is_utc(ts):
        return f"timestamp {ts!r} is not UTC"
    for i, signal in enumerate(_items(ctx.message.get("signals"))):
        sts = signal.get("timestamp") if isinstance(signal, dict) else None
        if isinstance(sts, str) and not _is_utc(sts):
            return f"signals[{i}].timestamp {sts!r} is not UTC"
    return None


@check("GEN-022")
def timestamp_in_future(ctx: Ctx) -> str | None:
    if ctx.msg_ts and ctx.gw_ts and ctx.msg_ts > ctx.gw_ts + timedelta(minutes=5):
        ahead = ctx.msg_ts - ctx.gw_ts
        hours = ahead.total_seconds() / 3600
        hint = " (whole hours: local time labelled UTC?)" if abs(hours - round(hours)) < 0.05 else ""
        return f"timestamp {ahead} after the gateway received it{hint}"
    return None


@check("GEN-023")
def timestamp_stale(ctx: Ctx) -> str | None:
    if ctx.msg_ts and ctx.gw_ts and ctx.gw_ts - ctx.msg_ts > timedelta(hours=1):
        return f"sent {ctx.gw_ts - ctx.msg_ts} after the event"
    return None


@check("GEN-025")
def odyssey_on_legacy(ctx: Ctx) -> str | None:
    if "odyssey" in ctx.message and game_branch(ctx.gameversion) == "legacy":
        return f"odyssey flag sent with Legacy gameversion {ctx.gv!r}"
    return None


@check("GEN-026")
def odyssey_without_horizons(ctx: Ctx) -> str | None:
    # CAPI-sourced horizons is inferred from the station (commodity-README:88-98), not
    # LoadGame, so it may legitimately disagree with the journal's odyssey flag.
    if ctx.gv.startswith("CAPI-"):
        return None
    if ctx.message.get("odyssey") is True and ctx.message.get("horizons") is False:
        return "odyssey true with horizons false"
    return None


@check("GEN-027")
def horizons_absent(ctx: Ctx) -> str | None:
    if ctx.schema_name == "blackmarket/1":   # the one schema that does not declare the flags
        return None
    return "horizons flag absent" if "horizons" not in ctx.message else None


@check("GEN-027J")
def flags_absent_journal(ctx: Ctx) -> str | None:
    # LoadGame carries Horizons in every client (Dev:378-419); only Odyssey can be absent (3.8).
    if "horizons" not in ctx.message:
        return ("horizons flag absent (odyssey sent without it)" if "odyssey" in ctx.message
                else "neither horizons nor odyssey sent")
    return None


@check("GEN-032")
def placeholder_starpos(ctx: Ctx) -> str | None:
    positions = [(ctx.message.get("StarPos"), ctx.system_name)]
    positions += [(hop.get("StarPos"), hop.get("StarSystem"))
                  for hop in _items(ctx.message.get("Route")) if isinstance(hop, dict)]
    for pos, name in positions:
        if (isinstance(pos, list) and len(pos) == 3 and all(v == 0 for v in pos)
                and name != "Sol"):
            return f"StarPos [0,0,0] for {name!r}"
    return None


def _positions(ctx: Ctx) -> Iterator[tuple[str, object, object, object]]:
    """(where, SystemAddress, StarPos, name) for the message and each NavRoute hop."""
    yield "", ctx.message.get("SystemAddress"), ctx.message.get("StarPos"), ctx.system_name
    for i, hop in enumerate(_items(ctx.message.get("Route"))):
        if isinstance(hop, dict):
            yield f"Route[{i}] ", hop.get("SystemAddress"), hop.get("StarPos"), hop.get("StarSystem")


@check("GEN-030")
def starpos_outside_system_address(ctx: Ctx) -> str | None:
    for where, address, pos, name in _positions(ctx):
        if not (isinstance(pos, list) and len(pos) == 3
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pos)):
            continue
        p = valid_pos(pos)
        if p is None:
            return f"{where}StarPos {str(pos)[:60]} for {name!r} is not a position in the galaxy"
        boxel = decode_id64(address)
        if boxel and not boxel.contains(p):
            # Says which fields disagree, not which one is wrong: in journal events the
            # SystemAddress usually comes from the game, while StarSystem/StarPos are often the
            # sender's own additions, and those are the ones that go stale after a jump.
            return (f"{where}StarPos {pos} and system name {name!r} do not belong to SystemAddress "
                    f"{address}: they are {boxel.distance_outside(p):.0f} ly outside the region it "
                    f"encodes, so one of them is from another system")
    return None


@check("GEN-033")
def system_name_not_address(ctx: Ctx) -> str | None:
    for where, address, _, name in _positions(ctx):
        if name_matches_address(name, address) is False:
            return (f"{where}system name {name!r} does not match its SystemAddress {address} "
                    "(one of them is from another system)")
    return None


@check("GEN-035")
def body_outside_system(ctx: Ctx) -> str | None:
    system, body = ctx.system_name, ctx.message.get("BodyName")
    if (isinstance(system, str) and isinstance(body, str) and body
            and is_procedural(system) and not (body == system or body.startswith(system + " "))):
        return f"BodyName {body!r} is not a body of {system!r} (stale location?)"
    return None


@check("GEN-034")
def personal_data(ctx: Ctx) -> str | None:
    path = first_key(ctx.message, lambda k: k in PERSONAL_KEYS)
    return f"{path} present (personal data)" if path else None


def _first_repeat(items: list) -> object | None:
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else repr(item)
        if key in seen:
            return item
        seen.add(key)
    return None


@check("GEN-040")
def repeated_unique_items(ctx: Ctx) -> str | None:
    """The gateway now accepts these as warnings ("accepted temporarily"), so they reach
    the relay although the schemas forbid them."""
    for key in UNIQUE_ARRAYS.get(ctx.schema_name, ()):
        dup = _first_repeat(_items(ctx.message.get(key)))
        if dup is not None:
            return f"{key} repeats {json.dumps(dup)[:80]}"
    if ctx.schema_name == "commodity/3":
        for item in _items(ctx.message.get("commodities")):
            if isinstance(item, dict):
                dup = _first_repeat(_items(item.get("statusFlags")))
                if dup is not None:
                    return f"statusFlags of {item.get('name')!r} repeats {dup!r}"
    return None


@check("GEN-042")
def operations_instance(ctx: Ctx) -> str | None:
    for key in ("MarketID", "marketId"):
        value = ctx.message.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 127_000_000 <= value < 128_000_000:
            return f"{key} {value} is an instanced Operations station, not part of the persistent galaxy"
    return None


@check("GEN-043")
def unreal_market_id(ctx: Ctx) -> str | None:
    if ctx.is_test:   # the test schemas are where made-up IDs belong (Dev:92-102)
        return None
    for key in ("MarketID", "marketId"):
        value = ctx.message.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value < 127_000_000:
            return (f"{key} {value} belongs to no real station: test data on a live schema, or an ID "
                    "damaged in a 32-bit field")
    return None


@check("GEN-041")
def no_market_sentinel(ctx: Ctx) -> str | None:
    for key in ("MarketID", "marketId"):
        value = ctx.message.get(key)
        if value == NO_MARKET:
            return f"{key} is the game's no-market value 2^64-1"
        # The same value rounded by a sender's 64-bit float (JSON in JavaScript): no real ID is near 2^63.
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 2 ** 63:
            return f"{key} is the game's no-market value 2^64-1, rounded by the sender to {value}"
    return None


# ------------------------------------------------------------ per schema

@check("JNL-001")
def codexentry_on_journal(ctx: Ctx) -> str | None:
    return "CodexEntry sent on journal/1; codexentry/1 is the specific schema" if ctx.event == "CodexEntry" else None


@check("COM-001")
def commodity_symbol_name(ctx: Ctx) -> str | None:
    for item in _items(ctx.message.get("commodities")):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and (name.startswith("$") or name.lower().endswith("_name;")):
            return f"commodity name {name!r} not stripped of $…_name;"
    return None


@check("COM-002")
def commodity_empty_lists(ctx: Ctx) -> str | None:
    # The README's rule is for journal-sourced markets (Market.json has neither list, so leave
    # them out rather than send them empty); CAPI data may legitimately carry an empty list.
    if ctx.gv.startswith("CAPI-"):
        return None
    empty = [k for k in ("economies", "prohibited") if ctx.message.get(k) == []]
    return f"empty {' and '.join(empty)} list sent" if empty else None


@check("COM-003")
def commodity_limpets(ctx: Ctx) -> str | None:
    for item in _items(ctx.message.get("commodities")):
        name = str(item.get("name", "")).lower().removeprefix("$").removesuffix("_name;") \
            if isinstance(item, dict) else ""
        if name == "drones":
            return "limpets (drones) included; NonMarketable items must be skipped"
    return None


@check("COM-004")
def commodity_station_type(ctx: Ctx) -> str | None:
    return "stationType present (README says remove; schema allows)" if "stationType" in ctx.message else None


@check("COM-005")
def commodity_capi_without_status_flags(ctx: Ctx) -> str | None:
    if not ctx.gv.startswith("CAPI-"):
        return None
    items = _items(ctx.message.get("commodities"))
    if items and not any(isinstance(i, dict) and "statusFlags" in i for i in items):
        return "CAPI market without any statusFlags"
    return None


@check("COM-006")
def commodity_colony_not_horizons(ctx: Ctx) -> str | None:
    # A CAPI fallback (commodity-README:88-94): a journal-sourced market takes horizons from
    # LoadGame, and so may a CAPI sender while the game is running.
    if not ctx.gv.startswith("CAPI-"):
        return None
    colony = any(isinstance(e, dict) and e.get("name") == "Colony"
                 for e in _items(ctx.message.get("economies")))
    if colony and ctx.message.get("horizons") is False:
        return "Colony economy but horizons false"
    return None


@check("COM-007")
def commodity_duplicates(ctx: Ctx) -> str | None:
    seen: set[str] = set()
    for item in _items(ctx.message.get("commodities")):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str):
            if name.lower() in seen:
                return f"commodity {name!r} listed more than once"
            seen.add(name.lower())
    return None


@check("OUT-001")
def outfitting_planet_approach_suite(ctx: Ctx) -> str | None:
    for module in _items(ctx.message.get("modules")):
        name = module.get("Name") if isinstance(module, dict) else module
        if isinstance(name, str) and name.lower() == "int_planetapproachsuite":
            return f"{name} included; it must be removed"
    return None


@check("CDX-001")
def codex_empty_body_name(ctx: Ctx) -> str | None:
    if "BodyName" in ctx.message:
        name = ctx.message["BodyName"]
        if not isinstance(name, str) or not name.strip():
            return f"BodyName {name!r} is empty; omit the key instead"
    return None


@check("CDX-002")
def codex_body_id_without_name(ctx: Ctx) -> str | None:
    # Stellar-body entries (found with the FSS) carry BodyID in the journal event itself;
    # the rule is about BodyID the sender adds from its own tracking.
    if ctx.message.get("Category") == "$Codex_Category_StellarBodies;":
        return None
    if "BodyID" in ctx.message and "BodyName" not in ctx.message:
        return "BodyID sent without BodyName"
    return None


@check("CDX-003")
def codex_surface_without_body(ctx: Ctx) -> str | None:
    if "Latitude" in ctx.message and "BodyName" not in ctx.message:
        return "surface entry (Latitude) without BodyName"
    return None


@check("FSD-001")
def signals_timestamp_mismatch(ctx: Ctx) -> str | None:
    signals = _items(ctx.message.get("signals"))
    if signals and isinstance(signals[0], dict):
        first = signals[0].get("timestamp")
        # Compared as times, not text: sub-second resolution is allowed (SR:77-81).
        top = ctx.message.get("timestamp")
        if first != top and (parse_ts(first) is None or parse_ts(first) != parse_ts(top)):
            return f"timestamp {ctx.message.get('timestamp')!r} is not the first signal's {first!r}"
    return None


@check("FSD-003")
def signals_out_of_order(ctx: Ctx) -> str | None:
    times = [parse_ts(s.get("timestamp")) for s in _items(ctx.message.get("signals"))
             if isinstance(s, dict)]
    for i in range(1, len(times)):
        if times[i] and times[i - 1] and times[i] < times[i - 1]:
            return f"signals[{i}] is earlier than signals[{i - 1}]"
    return None


@check("NAV-001")
def navroute_empty(ctx: Ctx) -> str | None:
    return "Route is empty" if ctx.message.get("Route") == [] else None


@check("NAV-002")
def navroute_repeated_hop(ctx: Ctx) -> str | None:
    hops = [h.get("SystemAddress") for h in _items(ctx.message.get("Route")) if isinstance(h, dict)]
    for i in range(1, len(hops)):
        if hops[i] is not None and hops[i] == hops[i - 1]:
            return f"Route[{i}] repeats SystemAddress {hops[i]}"
    return None


@check("APS-001")
def settlement_station_without_market(ctx: Ctx) -> str | None:
    # Station details with content mean a settlement with a market. Market-less sites
    # (Guardian ruins, visitor beacons) carry none, or an empty StationFaction ({}), which the
    # game writes for ruins and some senders pass through; that is no evidence of a market.
    station_keys = [k for k, v in ctx.message.items()
                    if k.startswith("Station") and v not in (None, "", {}, [])]
    if station_keys and "MarketID" not in ctx.message:
        return f"{station_keys[0]} sent but MarketID missing"
    return None


@check("SBC-001")
def barycentre_missing_elements(ctx: Ctx) -> str | None:
    # Each element is to be sent "if it's in the source data" (scanbarycentre-README:22-23), so
    # only a partial set, some elements kept and others dropped, shows a sender dropping them.
    missing = [k for k in ORBITAL_ELEMENTS if k not in ctx.message]
    return f"missing {', '.join(missing)}" if missing and len(missing) < len(ORBITAL_ELEMENTS) else None


@check("FSB-001")
def body_signals_empty(ctx: Ctx) -> str | None:
    return "Signals is empty" if ctx.message.get("Signals") == [] else None


@check("DCK-001")
def docking_station_name_empty(ctx: Ctx) -> str | None:
    name = ctx.message.get("StationName")
    return "StationName is empty" if isinstance(name, str) and not name.strip() else None


@check("FCJ-001")
def fcmaterials_journal_from_capi(ctx: Ctx) -> str | None:
    if ctx.gv.startswith("CAPI-") and not ctx.gv.endswith("-journal"):
        return f"CAPI gameversion {ctx.gv!r} on the journal-only schema"
    return None


@check("FCJ-002")
def fcmaterials_journal_extra_item_keys(ctx: Ctx) -> str | None:
    allowed = {"id", "Name", "Price", "Stock", "Demand"}
    for item in _items(ctx.message.get("Items")):
        if isinstance(item, dict):
            extra = sorted(set(item) - allowed)
            if extra:
                return f"Items[] has extra keys {extra}"
    return None


@check("FCJ-003")
def carrier_id_format(ctx: Ctx) -> str | None:
    cid = ctx.message.get("CarrierID")
    if isinstance(cid, str) and not CARRIER_ID.match(cid):
        return f"CarrierID {cid!r} is not a carrier callsign (X3F-N5Z or four characters)"
    return None


@check("FCC-001")
def fcmaterials_capi_extra_items(ctx: Ctx) -> str | None:
    items = ctx.message.get("Items")
    if isinstance(items, dict):
        extra = sorted(set(items) - {"purchases", "sales"})
        if extra:
            return f"Items has keys beyond purchases/sales: {extra[:5]}"
    return None


@check("FCC-002")
def fcmaterials_capi_items_shape(ctx: Ctx) -> str | None:
    items = ctx.message.get("Items")
    if not isinstance(items, dict):
        return f"Items is a {type(items).__name__}, not an object"
    missing = [k for k in ("purchases", "sales") if k not in items]
    if missing:
        return f"Items lacks {', '.join(missing)}"
    if not isinstance(items["purchases"], list):
        return "Items.purchases is not a list"
    return None


@check("FCC-003")
def fcmaterials_capi_locname(ctx: Ctx) -> str | None:
    path = first_key(ctx.message.get("Items"), lambda k: k == "locName")
    return f"Items.{path} present" if path else None


@check("FCC-004")
def fcmaterials_capi_sales_keys(ctx: Ctx) -> str | None:
    items = ctx.message.get("Items")
    sales = items.get("sales") if isinstance(items, dict) else None
    if isinstance(sales, dict):
        for key, entry in sales.items():
            if not key.isdigit():
                return f"sales key {key!r} is not a commodity id"
            if isinstance(entry, dict) and str(entry.get("id")) != key:
                return f"sales[{key!r}].id is {entry.get('id')!r}"
    return None


@check("BLK-001")
def blackmarket_deprecated(ctx: Ctx) -> str | None:
    return "blackmarket/1 is deprecated; use commodity/3 prohibited"


@check("BLK-002")
def blackmarket_personal(ctx: Ctx) -> str | None:
    found = [k for k in ("TotalSale", "AvgPricePaid", "StolenGoods", "BlackMarket")
             if k in ctx.message]
    return f"{', '.join(found)} must be removed" if found else None


@check("OUT-003")
def outfitting_v3_repeated_id(ctx: Ctx) -> str | None:
    """Keyed on id: regular, pre-engineered and merc-coin variants legitimately share a Name."""
    seen: set[object] = set()
    for module in _items(ctx.message.get("modules")):
        if isinstance(module, dict) and "id" in module:
            if module["id"] in seen:
                return f"module id {module['id']} listed more than once"
            seen.add(module["id"])
    return None


@check("SCO-001")
def scanorganic_body_not_renamed(ctx: Ctx) -> str | None:
    return "Body sent; it must be renamed to BodyID" if "Body" in ctx.message else None


@check("SCO-002")
def scanorganic_empty_body_name(ctx: Ctx) -> str | None:
    if "BodyName" in ctx.message:
        name = ctx.message["BodyName"]
        if not isinstance(name, str) or not name.strip():
            return f"BodyName {name!r} is empty; omit the key instead"
    return None


@check("SCO-004")
def latitude_longitude_range(ctx: Ctx) -> str | None:
    lat, lon = ctx.message.get("Latitude"), ctx.message.get("Longitude")
    if isinstance(lat, (int, float)) and not isinstance(lat, bool) and not -90 <= lat <= 90:
        return f"Latitude {lat} outside -90..90"
    if isinstance(lon, (int, float)) and not isinstance(lon, bool) and not -180 <= lon <= 180:
        return f"Longitude {lon} outside -180..180"
    return None


@check("SCO-005")
def scanorganic_variant_of_species(ctx: Ctx) -> str | None:
    species, variant = ctx.message.get("Species"), ctx.message.get("Variant")
    if isinstance(species, str) and isinstance(variant, str) and species.endswith("_Name;"):
        stem = species[: -len("Name;")]
        if not variant.startswith(stem):
            return f"Variant {variant!r} is not a variant of {species!r}"
    return None
