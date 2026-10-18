import datetime as dt

from eddn_validator import facts, pairing
from eddn_validator.subtypes import subtype

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 13, 12, tzinfo=UTC)


def test_same_tolerates_float_formatting_and_list_order():
    assert facts.same(7.75, 7.75000) and facts.same(3, 3.0)
    assert not facts.same(0.0277, 0.027703)          # EDRobot's rounding is a real mismatch
    assert facts.same([{"Name": "iron", "Percent": 1.0}, {"Name": "nickel", "Percent": 2.0}],
                      [{"Name": "nickel", "Percent": 2.0}, {"Name": "iron", "Percent": 1.0}])
    assert not facts.same(True, 1)


def test_system_pairing():
    known = {47102118899: ("Byeia Eur BH-C d13-1", [10737.84375, 375.5, 6458.25])}
    good = {"StarSystem": "Byeia Eur BH-C d13-1", "SystemAddress": 47102118899,
            "StarPos": [10737.84375, 375.5, 6458.25]}
    assert pairing.compare_system(good, known) == (2, 0, {})
    bad = dict(good, StarPos=[0.0, 0.0, 0.0])
    compared, mismatched, out = pairing.compare_system(bad, known)
    assert (compared, mismatched) == (2, 1) and "StarPos" in out
    assert pairing.compare_system(dict(good, SystemAddress=1), known) is None   # nothing known


def test_body_pairing_ignores_dynamic_fields_and_conflicts():
    known = {(1, 5): ("X 1", {"MassEM": 0.027703, "Radius": 1000.0})}
    msg = {"SystemAddress": 1, "BodyID": 5, "BodyName": "X 1", "MassEM": 0.0277,
           "Radius": 1000.0, "MeanAnomaly": 123.0, "DistanceFromArrivalLS": 9.0}
    compared, mismatched, out = pairing.compare_body(msg, known)
    assert (compared, mismatched) == (3, 1) and list(out) == ["MassEM"]


def test_station_pairing_aliases_and_symbol_names():
    known = {3228342528: ("Some Station", "Bernal", T0, None)}
    msg = {"MarketID": 3228342528, "StationName": "Some Station", "StationType": "Ocellus"}
    assert pairing.compare_station("journal/1:Docked", msg, T0, known) == (2, 0, {})
    megaship = {"MarketID": 3228342528, "StationName": "$Operations_Runner_Name:#index=1;"}
    assert pairing.compare_station("dockinggranted/1", megaship, T0, known) is None
    stale = {3228342528: ("Old Name", "ocellus", T0 - dt.timedelta(days=30), None)}
    assert pairing.compare_station("journal/1:Docked", msg, T0, stale) is None


def test_market_pairing_window_and_source():
    ref = {"gold": [100, 90, 5, 0], "silver": [50, 45, 1, 0]}
    snapshots = [(T0, "journal", ref), (T0, "capi", {"gold": [1, 1, 1, 1]})]
    msg = {"commodities": [{"name": "Gold", "buyPrice": 100, "sellPrice": 90, "stock": 5, "demand": 0},
                           {"name": "silver", "buyPrice": 51, "sellPrice": 45, "stock": 1, "demand": 0}]}
    compared, mismatched, out = pairing.compare_market(msg, T0 + dt.timedelta(minutes=2), "journal", snapshots)
    assert (compared, mismatched) == (3, 1) and "silver.buy/sell" in out
    assert pairing.compare_market(msg, T0 + dt.timedelta(minutes=30), "journal", snapshots) is None


def test_facts_batch_flags_conflicting_body_fields():
    batch = facts.Batch()
    scan = {"SystemAddress": 1, "BodyID": 2, "BodyName": "B", "MassEM": 1.0, "Radius": 5.0}
    batch.add("journal/1:Scan", scan, "live", "journal", "EDMC", T0, T0)
    batch.add("journal/1:Scan", dict(scan, MassEM=2.0), "live", "journal", "EDDiscovery", T0, T0)
    body = batch.bodies[(1, 2)]
    assert body["conflicts"] == {"MassEM"} and body["static"] == {"Radius": 5.0}


def test_subtypes():
    assert subtype("journal/1:Scan", {"StarType": "K", "ScanType": "AutoScan"}, "journal") == "star/autoscan"
    assert subtype("journal/1:Scan", {"PlanetClass": "Icy body", "ScanType": "Detailed"}, "journal") == "planet/detailed"
    assert subtype("journal/1:FSDJump", {"Factions": [{}]}, "journal") == "populated"
    assert subtype("journal/1:Docked", {"StationType": "FleetCarrier"}, "journal") == "fleetcarrier"
    assert subtype("commodity/3", {}, "capi") == "capi"
    assert subtype("navroute/1", {}, "journal") == "default"


def test_out_of_range_ids_are_ignored_not_fatal():
    huge = 2**64 + 5
    assert facts.systems_in({"StarSystem": "X", "SystemAddress": huge, "StarPos": [1, 2, 3]}) == []
    assert facts.station_in("journal/1:Docked", {"MarketID": huge, "StationName": "S"}) is None
    # Shared Operations placeholders and impossible low IDs never become station facts.
    assert facts.station_in("journal/1:Docked", {"MarketID": 127000256, "StationName": "S"}) is None
    assert facts.station_in("journal/1:Docked", {"MarketID": 42, "StationName": "Test Port"}) is None
    batch = facts.Batch()
    batch.add("journal/1:Scan", {"SystemAddress": 1, "BodyID": 2**40, "MassEM": 1.0},
              "live", "journal", "EDMC", T0, T0)
    assert batch.bodies == {}
    assert facts.db_int(-1) is None and facts.db_int(True) is None and facts.db_int(7) == 7


def test_station_in_the_wrong_system():
    known = {3228342528: ("Some Station", "coriolis", T0 - dt.timedelta(days=60), "Sol")}
    market = {"marketId": 3228342528, "systemName": "Achenar", "stationName": "Some Station",
              "commodities": []}
    compared, mismatched, out = pairing.compare_station("commodity/3", market, T0, known)
    assert (compared, mismatched) == (1, 1) and out["system"] == ["Achenar", "Sol"]   # old fact still counts
    assert pairing.compare_station("commodity/3", dict(market, systemName="Sol"), T0, known)[1] == 0


def test_moving_stations_are_not_tied_to_a_system():
    carrier = {3712345678: ("X9X-9XX", "surfacestation", T0, "Sol")}   # carrier mislabelled
    msg = {"marketId": 3712345678, "systemName": "Achenar", "stationName": "X9X-9XX", "commodities": []}
    result = pairing.compare_station("commodity/3", msg, T0, carrier)
    assert result is None or "system" not in result[2]
    assert facts.moving_station(3_600_000_123, None) and facts.moving_station(1, "fleetcarrier")
    assert not facts.moving_station(4_210_000_000, "coriolis")


def test_address_checked_by_name_and_position_only_where_no_rule_can():
    sol, pos = 10477373803, (0.0, 0.0, 0.0)          # mass code d: bits 35 and up are the N2 number
    by_name = {"Sol": [(sol, [0.0, 0.0, 0.0])],
               "LZK 12": [(10393389307, [1.0, 2.0, 3.0]), (44753127675, [9.0, 9.0, 9.0])]}
    wrong_n2 = sol | (1 << 35)                        # same boxel: GEN-030 cannot see it
    assert pairing.address_by_name(wrong_n2, "Sol", pos, by_name) == sol
    assert pairing.address_by_name(sol ^ (1 << 10), "Sol", pos, by_name) is None   # other boxel: GEN-030's
    assert pairing.address_by_name(wrong_n2, "Sol", (500.0, 0.0, 0.0), by_name) is None  # another Sol?
    assert pairing.address_by_name(123, "LZK 12", (5.0, 5.0, 5.0), by_name) is None      # name alone: never
    msg = {"StarSystem": "Sol", "SystemAddress": wrong_n2, "StarPos": [0, 0, 0]}
    assert pairing.compare_system(msg, {}, by_name) == (1, 1, {"SystemAddress": [wrong_n2, sol]})
    assert pairing.compare_system(msg, {}, {}) is None
    route = {"Route": [msg, {"StarSystem": "Sol", "SystemAddress": sol, "StarPos": [0, 0, 0]}]}
    assert pairing.compare_navroute(route, {sol: ("Sol", [0, 0, 0])}, by_name)[:2] == (3, 1)


def test_recycled_colonisation_ids_are_not_compared():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 17, 21, 28, tzinfo=timezone.utc)
    known = {3958572546: ("Orbital Construction Site: Rahman Relay", "SpaceConstructionDepot",
                          datetime(2026, 10, 11, tzinfo=timezone.utc), "Col 285 Sector WD-M b22-4"),
             4300000003: ("Planetary Construction Site: X", "PlanetaryConstructionDepot", now, "A"),
             128016640: ("Abraham Lincoln", "Orbis", now, "Sol")}
    ship = {"MarketID": 3958572546, "StationName": "$EXT_PANEL_ColonisationShip; Malaspina Depot",
            "StationType": "SurfaceStation", "StarSystem": "Col 359 Sector RS-T d3-106"}
    assert pairing.compare_station("journal/1:Docked", ship, now, known) is None
    built = {"MarketID": 4300000003, "StationName": "X Port", "StationType": "Outpost", "StarSystem": "A"}
    assert pairing.compare_station("journal/1:Docked", built, now, known) is None   # depot finished
    moved = {"MarketID": 128016640, "StationName": "Abraham Lincoln", "StationType": "Orbis", "StarSystem": "Lave"}
    assert pairing.compare_station("journal/1:Docked", moved, now, known)[1] == 1    # permanent: still checked


def test_station_judged_as_it_was_when_reported():
    from datetime import datetime, timedelta, timezone
    t = lambda d, h=0: datetime(2026, 10, d, h, tzinfo=timezone.utc)
    # 128016640 was Abraham Lincoln (Orbis) until Oct 15 17:00, Galileo (Ocellus) from Oct 15 18:04.
    galileo = ("Galileo", "Ocellus", t(18, 17), "Sol", "Abraham Lincoln", "orbis", t(15, 17), t(15, 18))
    old = {"MarketID": 128016640, "StationName": "Abraham Lincoln", "StationType": "Orbis", "StarSystem": "Sol"}
    new = {"MarketID": 128016640, "StationName": "Galileo", "StationType": "Ocellus", "StarSystem": "Sol"}
    known = {128016640: galileo}
    assert pairing.compare_station("journal/1:Docked", old, t(15, 10), known) == (3, 0, {})   # as it was
    assert pairing.compare_station("journal/1:Docked", new, t(17), known) == (3, 0, {})
    assert pairing.compare_station("journal/1:Docked", old, t(17), known)[1] == 2            # stale name
    between = pairing.compare_station("journal/1:Docked", old, t(15, 17) + timedelta(minutes=30), known)
    assert between == (1, 0, {})                       # unknown which state: system only
    assert facts.station_state_at(t(1), galileo[:3] + galileo[4:])[0] == "Abraham Lincoln"


def test_batch_marks_when_a_station_changes():
    from datetime import datetime, timezone
    t = lambda h: datetime(2026, 10, 15, h, tzinfo=timezone.utc)
    batch = facts.Batch()
    for h, name in ((10, "Abraham Lincoln"), (12, "Abraham Lincoln"), (18, "Galileo"), (19, "Galileo")):
        batch.add("journal/1:Docked", {"MarketID": 128016640, "StationName": name, "StationType": "Ocellus",
                                       "StarSystem": "Sol"}, "live", "journal", "EDMC", t(h), t(h))
    st = batch.stations[128016640]
    assert (st["name"], st["since"], st["last"]) == ("Galileo", t(18), t(19))


def test_what_counts_as_a_station_change():
    assert facts.station_changed("Abraham Lincoln", "orbis", "Galileo", "ocellus")
    assert facts.station_changed("Bethe City", "outpost", "Bethe City", "coriolis")
    assert not facts.station_changed("Bethe City", None, "Bethe City", "coriolis")      # type unknown
    assert not facts.station_changed("Two Jumps", "fleetcarrier", "Two Jumps", "surfacestation")
    assert not facts.station_changed("galileo", "ocellus", "Galileo", "ocellus")        # letter case


def test_station_judged_at_message_time_and_construction_names_skipped():
    from datetime import datetime, timezone
    known = {3715199232: ("K1K-64X", "FleetCarrier", datetime(2026, 9, 12, tzinfo=timezone.utc), None),
             4362485507: ("Allegro Centre", None, datetime(2026, 10, 1, tzinfo=timezone.utc), "X")}
    replay = {"MarketID": 3715199232, "StationName": "F1B-6VZ", "StationType": "FleetCarrier"}
    june, september = datetime(2026, 6, 10, tzinfo=timezone.utc), datetime(2026, 9, 12, tzinfo=timezone.utc)
    pairs = pairing.compare_all(replay, "journal/1:Docked", september, june, "journal", {}, {}, known, {})
    assert pairs["station"] is None                    # a June docking is not judged by September's record
    site = {"MarketID": 4362485507, "Name": "Planetary Construction Site: Goto Terminus", "StarSystem": "X"}
    assert pairing.compare_station("approachsettlement/1", site, datetime(2026, 10, 1, tzinfo=timezone.utc), known) is None


def test_system_compared_only_for_the_same_station_and_generic_type_skipped():
    from datetime import datetime, timezone
    aug, april = datetime(2026, 8, 15, tzinfo=timezone.utc), datetime(2026, 4, 18, tzinfo=timezone.utc)
    known = {4353087235: ("Ricardo Platform", "Outpost", aug, "Shui Wei Sector JI-S b4-7")}
    other = {"marketId": 4353087235, "stationName": "Eesuola Dock", "systemName": "Col 285 Sector TP-O d6-19"}
    assert pairing.compare_station("commodity/3", other, april, known) is None       # reissued ID
    moved = {"marketId": 4353087235, "stationName": "Ricardo Platform", "systemName": "Col 285 Sector TP-O d6-19"}
    assert pairing.compare_station("commodity/3", moved, april, known)[1] == 1         # same station, wrong system
    generic = {"MarketID": 4353087235, "StationName": "Ricardo Platform", "StationType": "SurfaceStation",
               "StarSystem": "Shui Wei Sector JI-S b4-7"}
    assert pairing.compare_station("journal/1:Docked", generic, aug, known) == (2, 0, {})
