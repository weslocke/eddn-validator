import copy
from pathlib import Path

import pytest
import yaml

from eddn_validator.rules import REGISTRY, evaluate, load_rules

CONFIG = yaml.safe_load((Path(__file__).parents[1] / "config" / "rules.yaml").read_text())
ALL_RULES = load_rules({"rules": {k: {**v, "enabled": True} for k, v in CONFIG["rules"].items()}})
SCHEMA = "https://eddn.edcd.io/schemas/{}"

HEADER = {
    "uploaderID": "a" * 40,
    "softwareName": "TestApp",
    "softwareVersion": "1.2.3",
    "gameversion": "4.4.1.1",
    "gamebuild": "r332841/r0 ",
    "gatewayTimestamp": "2026-10-13T12:00:05.000000Z",
}
TS = "2026-10-13T12:00:00Z"
POS = {"StarSystem": "Byeia Eur BH-C d13-1", "SystemAddress": 47102118899, "StarPos": [10737.84375, 375.5, 6458.25]}
FLAGS = {"horizons": True, "odyssey": True}

CLEAN = {
    "journal/1": {"timestamp": TS, "event": "FSDJump", **POS, **FLAGS},
    "commodity/3": {"timestamp": TS, "systemName": "Sol", "stationName": "Abraham Lincoln",
                    "marketId": 128016640, "commodities": [
                        {"name": "gold", "meanPrice": 1, "buyPrice": 1, "stock": 1, "stockBracket": 1,
                         "sellPrice": 1, "demand": 1, "demandBracket": 1}], **FLAGS},
    "outfitting/2": {"timestamp": TS, "systemName": "Sol", "stationName": "X", "marketId": 3228342528,
                     "modules": ["Hpt_pulselaser_fixed_small"], **FLAGS},
    "codexentry/1": {"timestamp": TS, "event": "CodexEntry", "System": "Sol", "StarPos": [0, 0, 0],
                     "SystemAddress": 10477373803, "EntryID": 1, **FLAGS},
    "fsssignaldiscovered/1": {"timestamp": TS, "event": "FSSSignalDiscovered", **POS, **FLAGS,
                              "signals": [{"timestamp": TS, "SignalName": "A"},
                                          {"timestamp": "2026-10-13T12:00:01Z", "SignalName": "B"}]},
    "navroute/1": {"timestamp": TS, "event": "NavRoute", **FLAGS, "Route": [
        {"StarSystem": "Sol", "SystemAddress": 10477373803, "StarPos": [0, 0, 0], "StarClass": "G"},
        {"StarSystem": "Byeia Eur BH-C d13-1", "SystemAddress": 47102118899,
         "StarPos": [10737.84375, 375.5, 6458.25], "StarClass": "M"}]},
    "approachsettlement/1": {"timestamp": TS, "event": "ApproachSettlement", **POS, **FLAGS, "Name": "S",
                             "BodyID": 1, "BodyName": "Byeia Eur BH-C d13-1 3", "Latitude": 1.0, "Longitude": 2.0,
                             "MarketID": 3520000000, "StationFaction": {"Name": "F"}},
    "scanbarycentre/1": {"timestamp": TS, "event": "ScanBaryCentre", **POS, **FLAGS, "BodyID": 1,
                         **{k: 1.0 for k in ("SemiMajorAxis", "Eccentricity", "OrbitalInclination",
                                             "Periapsis", "OrbitalPeriod", "AscendingNode", "MeanAnomaly")}},
    "fssbodysignals/1": {"timestamp": TS, "event": "FSSBodySignals", **POS, **FLAGS, "BodyID": 1,
                         "Signals": [{"Type": "$SAA_SignalType_Geological;", "Count": 2}]},
    "dockinggranted/1": {"timestamp": TS, "event": "DockingGranted", "MarketID": 3228342528, "StationName": "X",
                         "LandingPad": 3, **FLAGS},
    "fcmaterials_journal/1": {"timestamp": TS, "event": "FCMaterials", "MarketID": 3700000000, "CarrierName": "C",
                              "CarrierID": "X3F-N5Z", **FLAGS, "Items": [
                                  {"id": 1, "Name": "$x_name;", "Price": 1, "Stock": 1, "Demand": 0}]},
    "outfitting/3": {"timestamp": TS, "systemName": "Sol", "stationName": "X", "marketId": 3228342528, **FLAGS,
                     "modules": [{"id": 1, "Name": "hpt_pulselaser_fixed_small", "BuyPrice": 1, "BuyMercCoinsPrice": 0},
                                 {"id": 2, "Name": "hpt_pulselaser_fixed_small", "BuyPrice": 2, "BuyMercCoinsPrice": 5}]},
    "shipyard/2": {"timestamp": TS, "systemName": "Sol", "stationName": "X", "marketId": 3228342528, **FLAGS,
                   "ships": ["sidewinder", "eagle"]},
    "scanorganic/1": {"timestamp": TS, "event": "ScanOrganic", **POS, **FLAGS, "ScanType": "Sample",
                      "Genus": "$Codex_Ent_Bacterial_Genus_Name;", "Species": "$Codex_Ent_Bacterial_01_Name;",
                      "Variant": "$Codex_Ent_Bacterial_01_K_Name;", "BodyID": 3,
                      "BodyName": "Byeia Eur BH-C d13-1 3", "Latitude": 12.5, "Longitude": -170.0},
    "fcmaterials_capi/1": {"timestamp": TS, "event": "FCMaterials", "MarketID": 3700000000, "CarrierID": "X3F-N5Z",
                           **FLAGS, "Items": {"purchases": [], "sales": {"7": {"id": 7, "name": "x",
                                                                               "price": 1, "stock": 1}}}},
}
CAPI_GV = {"commodity/3": "CAPI-Live-market", "fcmaterials_capi/1": "CAPI-Live-market"}


def envelope(schema, message=None, **header):
    h = {**HEADER, "gameversion": CAPI_GV.get(schema, HEADER["gameversion"]), **header}
    if schema in CAPI_GV:
        h["gamebuild"] = ""
    h = {k: v for k, v in h.items() if v is not ...}
    return {"$schemaRef": SCHEMA.format(schema), "header": h,
            "message": copy.deepcopy(CLEAN[schema] if message is None else message)}


def ids(env):
    return {f.rule for f in evaluate(ALL_RULES, env)}


def test_yaml_and_checks_agree():
    assert set(CONFIG["rules"]) == set(REGISTRY)


def test_disabled_rules_are_dropped():
    enabled = {r.id for r in load_rules(CONFIG)}
    assert {"COM-004", "CDX-002"}.isdisjoint(enabled) and "GEN-020" in enabled


@pytest.mark.parametrize("schema", sorted(CLEAN))
def test_clean_messages_have_no_findings(schema):
    env = envelope(schema)
    if schema == "commodity/3":   # CAPI-sourced market: statusFlags expected
        env["message"]["commodities"][0]["statusFlags"] = ["Producer"]
    assert ids(env) == set()


def mutate(schema, change, **header):
    env = envelope(schema, **header)
    change(env["message"])
    return env


CASES = [
    ("GEN-001", envelope("journal/1", gameversion=...)),
    ("GEN-002", envelope("journal/1", gamebuild=...)),
    ("GEN-003", envelope("journal/1", gameversion="Odyssey")),
    ("GEN-003B", envelope("commodity/3", gameversion="CAPI-market")),
    ("GEN-004", envelope("commodity/3", gameversion="CAPI-Live-shipyard")),
    ("GEN-004", envelope("journal/1", gameversion="CAPI-Live-market")),
    ("GEN-005", envelope("journal/1", gamebuild="CAPI-Live-journal")),
    ("GEN-006", envelope("journal/1", gamebuild="r332668/r0")),
    ("GEN-007", envelope("journal/1", gameversion="")),
    ("GEN-008", envelope("fcmaterials_capi/1", gameversion="4.4.1.0")),
    ("GEN-010", envelope("journal/1", softwareName="  ")),
    ("GEN-011", envelope("journal/1", softwareVersion="")),
    ("GEN-011B", envelope("journal/1", softwareVersion="latest")),
    ("GEN-014", envelope("journal/1", gameversion="4.5.0.0 Beta")),
    ("GEN-020", mutate("journal/1", lambda m: m.update(Genuses=[{"Genus": "x", "Genus_Localised": "X"}]))),
    ("GEN-021", mutate("journal/1", lambda m: m.update(timestamp="2026-10-13T08:00:00-04:00"))),
    ("GEN-022", mutate("journal/1", lambda m: m.update(timestamp="2026-10-13T16:00:05Z"))),
    ("GEN-023", mutate("journal/1", lambda m: m.update(timestamp="2026-10-12T12:00:00Z"))),
    ("GEN-025", envelope("journal/1", gameversion="3.8.0.407")),
    ("GEN-026", mutate("journal/1", lambda m: m.update(horizons=False))),
    ("GEN-027", mutate("commodity/3", lambda m: m.pop("horizons"))),
    ("GEN-027J", mutate("journal/1", lambda m: (m.pop("horizons"), m.pop("odyssey")))),
    ("GEN-030", mutate("journal/1", lambda m: m.update(StarSystem="Sol", SystemAddress=10477373803,
                                                       StarPos=[100.0, 0.0, 0.0]))),
    ("GEN-030", mutate("navroute/1", lambda m: m["Route"][1].update(StarPos=[0, 0, 0]))),
    ("GEN-032", mutate("journal/1", lambda m: m.update(StarPos=[0, 0, 0]))),
    ("GEN-032", mutate("navroute/1", lambda m: m["Route"][1].update(StarPos=[0, 0, 0]))),
    ("GEN-034", mutate("journal/1", lambda m: m.update(Factions=[{"Name": "F", "MyReputation": 5.0}]))),
    ("JNL-001", mutate("journal/1", lambda m: m.update(event="CodexEntry"))),
    ("COM-001", mutate("commodity/3", lambda m: m["commodities"][0].update(name="$gold_name;"))),
    ("COM-002", mutate("commodity/3", lambda m: m.update(prohibited=[]))),
    ("COM-003", mutate("commodity/3", lambda m: m["commodities"][0].update(name="Drones"))),
    ("COM-005", envelope("commodity/3")),
    ("COM-006", mutate("commodity/3", lambda m: m.update(horizons=False, odyssey=False,
                                                       economies=[{"name": "Colony", "proportion": 1}]))),
    ("COM-007", mutate("commodity/3", lambda m: m["commodities"].append(dict(m["commodities"][0], name="GOLD")))),
    ("OUT-001", mutate("outfitting/2", lambda m: m["modules"].append("Int_PlanetApproachSuite"))),
    ("CDX-001", mutate("codexentry/1", lambda m: m.update(BodyName=" "))),
    ("CDX-002", mutate("codexentry/1", lambda m: m.update(BodyID=3))),
    ("CDX-003", mutate("codexentry/1", lambda m: m.update(Latitude=1.0, Longitude=2.0))),
    ("FSD-001", mutate("fsssignaldiscovered/1", lambda m: m.update(timestamp="2026-10-13T12:00:01Z"))),
    ("FSD-003", mutate("fsssignaldiscovered/1", lambda m: m["signals"].reverse())),
    ("NAV-001", mutate("navroute/1", lambda m: m.update(Route=[]))),
    ("NAV-002", mutate("navroute/1", lambda m: m["Route"][1].update(SystemAddress=10477373803))),
    ("APS-001", mutate("approachsettlement/1", lambda m: m.pop("MarketID"))),
    ("SBC-001", mutate("scanbarycentre/1", lambda m: m.pop("MeanAnomaly"))),
    ("FSB-001", mutate("fssbodysignals/1", lambda m: m.update(Signals=[]))),
    ("DCK-001", mutate("dockinggranted/1", lambda m: m.update(StationName=""))),
    ("FCJ-001", envelope("fcmaterials_journal/1", gameversion="CAPI-Live-market")),
    ("FCJ-002", mutate("fcmaterials_journal/1", lambda m: m["Items"][0].update(Name_Localised="x"))),
    ("FCJ-003", mutate("fcmaterials_journal/1", lambda m: m.update(CarrierID="my carrier"))),
    ("FCC-001", mutate("fcmaterials_capi/1", lambda m: m["Items"].update(orders={}))),
    ("FCC-002", mutate("fcmaterials_capi/1", lambda m: m["Items"].pop("sales"))),
    ("FCC-003", mutate("fcmaterials_capi/1", lambda m: m["Items"]["sales"]["7"].update(locName="X"))),
    ("FCC-004", mutate("fcmaterials_capi/1", lambda m: m["Items"]["sales"]["7"].update(id=8))),
    ("GEN-014B", envelope("journal/1", gameversion="4.4.1.0", gamebuild="r332668/r0 ")),
    ("GEN-040", mutate("outfitting/2", lambda m: m["modules"].append("Hpt_pulselaser_fixed_small"))),
    ("GEN-040", mutate("shipyard/2", lambda m: m["ships"].append("eagle"))),
    ("GEN-040", mutate("commodity/3", lambda m: m["commodities"][0].update(statusFlags=["powerplay", "powerplay"]))),
    ("GEN-040", mutate("commodity/3", lambda m: m.update(prohibited=["Slaves", "Slaves"]))),
    ("GEN-040", mutate("outfitting/3", lambda m: m["modules"].append(dict(m["modules"][0])))),
    ("GEN-041", mutate("journal/1", lambda m: m.update(event="Docked", MarketID=2**64 - 1, StationName=""))),
    ("OUT-003", mutate("outfitting/3", lambda m: m["modules"][1].update(id=1))),
    ("GEN-042", mutate("dockinggranted/1", lambda m: m.update(MarketID=127000256))),
    ("GEN-043", mutate("journal/1", lambda m: m.update(event="Docked", MarketID=42, StationName="Test Port"))),
    ("SCO-001", mutate("scanorganic/1", lambda m: m.update(Body=3))),
    ("SCO-002", mutate("scanorganic/1", lambda m: m.update(BodyName=""))),
    ("GEN-035", mutate("scanorganic/1", lambda m: m.update(BodyName="Other Sector AB-C d1-2 4"))),
    ("GEN-035", mutate("journal/1", lambda m: m.update(event="Scan", BodyName="Wrongsys AB-C d1 2"))),
    ("GEN-033", mutate("journal/1", lambda m: m.update(StarSystem="Byeia Eur BH-C d13-2"))),
    ("GEN-033", mutate("navroute/1", lambda m: m["Route"][1].update(StarSystem="Byeia Eur BH-C d14-1"))),
    ("SCO-004", mutate("scanorganic/1", lambda m: m.update(Latitude=95.0))),
    ("SCO-004", mutate("codexentry/1", lambda m: m.update(Latitude=1.0, Longitude=200.0, BodyName="B"))),
    ("SCO-005", mutate("scanorganic/1", lambda m: m.update(Variant="$Codex_Ent_Tussocks_01_K_Name;"))),
]


@pytest.mark.parametrize("rule_id, env", CASES, ids=[f"{r}-{i}" for i, (r, _) in enumerate(CASES)])
def test_rule_fires(rule_id, env):
    assert rule_id in ids(env)


def test_every_check_has_a_case():
    untested = set(REGISTRY) - {r for r, _ in CASES} - {"COM-004", "BLK-001", "BLK-002"}
    assert untested == set()


def test_rules_scoped_to_schemas():
    # OUT-001 must not look at journal messages; JNL-001 must not fire on codexentry/1.
    env = mutate("journal/1", lambda m: m.update(modules=["Int_PlanetApproachSuite"]))
    assert "OUT-001" not in ids(env)
    assert "JNL-001" not in ids(envelope("codexentry/1"))


def test_refinements_from_real_traffic():
    # Stellar-body codex entries carry BodyID natively.
    native = mutate("codexentry/1", lambda m: m.update(BodyID=1, Category="$Codex_Category_StellarBodies;"))
    assert "CDX-002" not in ids(native)
    # Squadron carriers have four-character IDs.
    assert "FCJ-003" not in ids(mutate("fcmaterials_capi/1", lambda m: m.update(CarrierID="R312")))
    # CAPI horizons comes from the station, so it may disagree with odyssey.
    capi = mutate("commodity/3", lambda m: m.update(horizons=False))
    assert "GEN-026" not in ids(capi)


def test_live_builds_and_reused_beta_build_are_not_beta():
    assert "GEN-014B" not in ids(envelope("journal/1"))
    assert "GEN-014B" not in ids(envelope("journal/1", gameversion="4.4.0.0", gamebuild="r330116/r0 "))
    test_env = envelope("journal/1", gameversion="4.4.1.0", gamebuild="r332668/r0 ")
    test_env["$schemaRef"] += "/test"
    assert "GEN-014B" not in ids(test_env)


def test_v3_modules_may_share_a_name():
    # Regular and merc-coin variants share a Name but differ in id: legitimate in outfitting/3.
    assert {"GEN-040", "OUT-003"}.isdisjoint(ids(envelope("outfitting/3")))


def test_future_whole_hours_hint():
    env = mutate("journal/1", lambda m: m.update(timestamp="2026-10-13T16:00:05Z"))
    detail = next(f.detail for f in evaluate(ALL_RULES, env) if f.rule == "GEN-022")
    assert "local time" in detail


def test_eddn_repo_fixtures_load_without_errors():
    """The EDDN repo's own hand-built valid messages must not crash any check."""
    fixtures = Path(__file__).parent / "fixtures" / "eddn"
    files = sorted(fixtures.glob("*.json"))
    assert files
    import json
    for path in files:
        evaluate(ALL_RULES, json.loads(path.read_text()))


def test_aps001_ignores_an_empty_station_faction_at_ruins():
    from eddn_validator.config import Settings
    from eddn_validator.rules import evaluate, load_rules
    rules = [r for r in load_rules(Settings.from_env().load_yaml("rules.yaml")) if r.id == "APS-001"]
    def env(message):
        return {"$schemaRef": "https://eddn.edcd.io/schemas/approachsettlement/1",
                "header": {"softwareName": "x", "softwareVersion": "1"}, "message": message}
    ruins = {"Name": "$Ancient_Small_005:#index=1;", "BodyID": 44, "StationFaction": {}}
    assert not evaluate(rules, env(ruins))                                    # Guardian ruins: no market
    settlement = {"Name": "Rogers Hold", "StationFaction": {"Name": "Some Faction"}}
    assert evaluate(rules, env(settlement))                                   # real settlement, MarketID missing
    assert not evaluate(rules, env({**settlement, "MarketID": 3_900_000_001}))
