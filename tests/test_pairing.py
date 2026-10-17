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
