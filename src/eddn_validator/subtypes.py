"""Subtypes: messages of one event type that legitimately differ in shape (docs/DESIGN.md §6.2).

A Scan of a star has StarType and StellarMass, a planet PlanetClass and
Atmosphere; a populated FSDJump has Factions. Comparing them as one population
would make every optional-by-kind field look "sometimes missing".
"""

from __future__ import annotations

PLANETARY = {
    "crateroutpost", "craterport", "onfootsettlement", "surfacestation",
    "planetaryconstructiondepot", "dockableplanetstation",
}
ORBITAL = {
    "coriolis", "orbis", "ocellus", "outpost", "bernal", "asteroidbase", "megaship",
    "spaceconstructiondepot", "gameplaypoi", "dodec",
}
SOURCE_SPLIT = {"commodity/3", "outfitting/2", "outfitting/3", "shipyard/2"}


def _scan_class(scan_type: object) -> str:
    if not isinstance(scan_type, str):
        return "unknown"
    if scan_type.startswith("NavBeacon"):
        return "navbeacon"
    if scan_type in ("Detailed", "AutoScan", "Basic"):
        return scan_type.lower()
    return "other"


def subtype(event_type: str, message: dict, source: str) -> str:
    if event_type == "journal/1:Scan":
        kind = ("star" if "StarType" in message
                else "planet" if "PlanetClass" in message else "belt")
        return f"{kind}/{_scan_class(message.get('ScanType'))}"
    if event_type in ("journal/1:FSDJump", "journal/1:CarrierJump"):
        return "populated" if message.get("Factions") else "unpopulated"
    if event_type == "journal/1:Location":
        where = "docked" if message.get("Docked") else "space"
        return f"{where}/{'populated' if message.get('Factions') else 'unpopulated'}"
    if event_type == "journal/1:Docked":
        station = str(message.get("StationType", "")).lower()
        if station == "fleetcarrier":
            return "fleetcarrier"
        if station in PLANETARY:
            return "planetary"
        if station in ORBITAL:
            return "orbital"
        return "other"
    if event_type in SOURCE_SPLIT:
        return source
    return "default"
