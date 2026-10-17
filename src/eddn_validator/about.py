"""Plain-English content for the /about/<layer> pages.

The lists are built from the same configuration the scorer uses (config/rules.yaml,
config/scoring.yaml, the body fields compared in facts.py), so the pages cannot drift
from what is actually checked. Only the wording lives here.
"""

from __future__ import annotations

from .config import Settings
from .facts import BODY_STATIC

LAYERS = ("rules", "fields", "values", "pairing", "behaviour")

SEVERITY = {
    "error": ("Serious", "counts fully"),
    "warning": ("Minor", "counts half"),
    "info": ("Note", "shown, not scored"),
}

# One short, non-technical sentence per rule, grouped as the page shows them.
RULE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Labelling the data", [
        ("GEN-001", "Says which game version the data came from."),
        ("GEN-002", "Says which game build the data came from."),
        ("GEN-003", "The game version looks like a real game version."),
        ("GEN-003B", "Data from Frontier's online service says whether it is the live or legacy game."),
        ("GEN-004", "Data from Frontier's online service is labelled with the right kind (market or shipyard)."),
        ("GEN-005", "Only data from the online service may copy its label into the build field."),
        ("GEN-006", "The game build is copied exactly as the game wrote it."),
        ("GEN-007", "The game version is not left blank."),
        ("GEN-008", "Online-service carrier data is not labelled with a game version from the player's files."),
        ("GEN-010", "The app's name is filled in."),
        ("GEN-011", "The app's version is filled in."),
        ("GEN-011B", "The app's version looks like a version number."),
        ("GEN-014", "Data from a test (beta) version of the game goes to the test channel."),
        ("GEN-014B", "Data from a known beta build of the game goes to the test channel."),
    ]),
    ("Where things are", [
        ("GEN-030", "A star system's position matches the part of the galaxy its system ID points to "
                    "(catches a name and position left over from the previous system after a jump)."),
        ("GEN-033", "A star system's name matches its system ID (for names the game generates)."),
        ("GEN-035", "A planet or moon belongs to the star system it is reported in."),
        ("GEN-032", "No made-up 0, 0, 0 position for any system other than Sol."),
        ("GEN-043", "The market ID belongs to a real station (not test data, not a damaged number)."),
        ("GEN-042", "Data from a temporary mission instance, not the permanent galaxy."),
        ("GEN-041", "The game's own \"no market here\" value."),
    ]),
    ("What every message must and must not contain", [
        ("GEN-020", "No translated display text: only the game's internal names."),
        ("GEN-034", "No personal details about the player, such as fines, reputation or ship name."),
        ("GEN-040", "Lists that must not repeat (modules, ships, banned goods) have no item twice."),
        ("GEN-021", "Times are given in universal time (UTC)."),
        ("GEN-022", "Times are not in the future."),
        ("GEN-023", "Data sent more than an hour after it happened."),
        ("GEN-025", "The Odyssey flag is left out for the old Legacy game, which has no Odyssey."),
        ("GEN-026", "The Horizons and Odyssey flags make sense together."),
        ("GEN-027", "The Horizons flag is included where the format allows it."),
        ("GEN-027J", "Journal messages include the Horizons and Odyssey flags."),
        ("JNL-001", "Codex discoveries are sent on the codex channel, not the general one."),
    ]),
    ("Markets, outfitting and shipyards", [
        ("COM-001", "Commodity names are cleaned of the game's internal codes."),
        ("COM-002", "Empty lists are left out instead of sent empty."),
        ("COM-003", "Limpets are not listed as a commodity."),
        ("COM-005", "Market data from the online service includes its status flags."),
        ("COM-006", "A station with a Colony economy is marked as Horizons."),
        ("COM-007", "Each commodity is listed once."),
        ("OUT-001", "The planetary approach suite, which must always be removed, is not listed."),
        ("OUT-003", "Each module is listed once."),
    ]),
    ("Exploration and travel", [
        ("CDX-001", "Codex discoveries never send a blank planet name."),
        ("CDX-003", "Codex discoveries on a planet's surface name the planet."),
        ("FSD-001", "A batch of signals is stamped with the first signal's time."),
        ("FSD-003", "Signals in a batch are in time order."),
        ("NAV-001", "A route is not sent empty."),
        ("NAV-002", "A route never lists the same system twice in a row."),
        ("APS-001", "Settlements include their market ID when they have one."),
        ("SBC-001", "Barycentre (orbit centre) scans include every orbit detail."),
        ("FSB-001", "A planet's signal list is not sent empty."),
        ("DCK-001", "Docking messages include the station's name."),
        ("SCO-001", "Biology scans use the correct name for the planet's ID."),
        ("SCO-002", "Biology scans never send a blank planet name."),
        ("SCO-004", "Latitude and longitude on a planet are within range."),
        ("SCO-005", "A species' variant really is a variant of that species."),
    ]),
    ("Fleet carriers", [
        ("FCJ-001", "Carrier materials from the game files are not labelled as online-service data."),
        ("FCJ-002", "Carrier materials contain only the expected item details."),
        ("FCJ-003", "The carrier ID looks like a carrier callsign."),
        ("FCC-001", "Online-service carrier data holds only the buy and sell lists."),
        ("FCC-002", "Online-service carrier data has both its buy and sell lists."),
        ("FCC-003", "Internal display names are removed from carrier data."),
        ("FCC-004", "Each item for sale is listed under its own item number."),
    ]),
    ("Old formats", [
        ("BLK-001", "The old black-market format, replaced in 2017, is not used."),
        ("BLK-002", "Personal sale details are removed from old black-market messages."),
    ]),
]

SWITCHED_OFF = {
    "COM-004": "EDDN's documentation contradicts itself about the station type in market data.",
    "CDX-002": "The game now includes the planet ID itself, so the old rule no longer applies.",
    "COM-005": "Frontier's online service only sometimes includes status flags, so a market without "
               "them is usually correct.",
}

SUBTYPES = [
    ("Star and planet scans", "Stars, planets and asteroid belts, each split by how they were scanned "
                              "(detailed scan, automatic scan, basic scan, navigation beacon)."),
    ("Jumps and carrier jumps", "Systems with a population and systems without."),
    ("Location reports", "Docked or in space, each with or without a population."),
    ("Docking", "Fleet carriers, planetary ports, space stations, and other."),
    ("Markets, outfitting and shipyards", "Read from the game's files, or fetched from Frontier's online service."),
    ("Everything else", "One group per kind of message."),
]

BODY_FIELD_NAMES = {
    "StarType": "star type", "Subclass": "star subclass", "StellarMass": "star mass",
    "AbsoluteMagnitude": "brightness", "Luminosity": "luminosity class", "Age_MY": "age",
    "PlanetClass": "planet type", "MassEM": "mass", "Radius": "size",
    "SurfaceGravity": "gravity", "SurfaceTemperature": "temperature",
    "SurfacePressure": "surface pressure", "Atmosphere": "atmosphere",
    "AtmosphereType": "atmosphere type", "AtmosphereComposition": "atmosphere make-up",
    "Composition": "rock, metal and ice make-up", "Materials": "surface materials",
    "Rings": "rings", "RotationPeriod": "length of day", "AxialTilt": "axial tilt",
    "SemiMajorAxis": "orbit size", "Eccentricity": "orbit shape",
    "OrbitalInclination": "orbit tilt", "Periapsis": "closest approach",
    "OrbitalPeriod": "length of year", "AscendingNode": "orbit orientation",
    "TidalLock": "tidally locked", "Landable": "landable", "Volcanism": "volcanism",
    "TerraformState": "terraforming", "ReserveLevel": "ring reserves",
    "Parents": "what it orbits",
}

BODY_NOT_COMPARED = [
    ("Where it is on its orbit right now", "changes all the time"),
    ("Distance from the arrival star", "changes as the body moves along its orbit"),
    ("Whether someone discovered, mapped or landed on it first", "depends on the player"),
]

NEVER_VALUE_CHECKED = [
    "Names of star systems, planets, stations, carriers, signals and factions",
    "Times and dates",
    "ID numbers (system, body, market, codex entry)",
    "The ID numbers of the bodies a planet orbits",
]


def context(settings: Settings, layer: str) -> dict:
    scoring = settings.load_yaml("scoring.yaml")
    rules_cfg = settings.load_yaml("rules.yaml").get("rules", {})
    weights = scoring["weights"]
    ctx: dict = {"layer": layer, "weight": weights.get(
        {"fields": "structure"}.get(layer, layer), 0), "weights": weights}
    if layer == "rules":
        groups = []
        for title, items in RULE_GROUPS:
            rows = []
            for rule_id, text in items:
                cfg = rules_cfg.get(rule_id)
                if not cfg or not cfg.get("enabled", True):
                    continue
                label, effect = SEVERITY[cfg["severity"]]
                rows.append({"id": rule_id, "text": text, "severity": cfg["severity"],
                             "label": label, "effect": effect, "source": cfg.get("source", "")})
            if rows:
                groups.append({"title": title, "rules": rows})
        listed = {r["id"] for g in groups for r in g["rules"]}
        missing = sorted(k for k, v in rules_cfg.items()
                         if v.get("enabled", True) and k not in listed)
        ctx.update(groups=groups, switched_off=SWITCHED_OFF, unlisted=missing,
                   total=len(listed))
    elif layer == "fields":
        ctx.update(subtypes=SUBTYPES, expected=round(100 * scoring["expected_threshold"]),
                   common=round(100 * scoring["common_threshold"]),
                   ref_min=scoring["reference_min_messages"])
    elif layer == "values":
        v = scoring["values"]
        ctx.update(never=NEVER_VALUE_CHECKED, enum_max=v["enum_max_distinct"],
                   enum_min=v["enum_min_samples"], margin=round(100 * v["numeric_margin"]),
                   far=int(v["numeric_margin_warning"]))
    elif layer == "pairing":
        ctx.update(body_fields=sorted({BODY_FIELD_NAMES.get(f, f) for f in BODY_STATIC}),
                   body_not=BODY_NOT_COMPARED)
    elif layer == "behaviour":
        ctx.update(stale=scoring["stale_after_minutes"], future=scoring["future_after_minutes"],
                   burst=scoring["burst_per_second"],
                   excluded=", ".join(scoring.get("behaviour_baseline_exclude", [])))
    return ctx


RULE_FAMILIES = {
    "GEN": "General (every schema)", "COM": "Commodity", "OUT": "Outfitting", "SHP": "Shipyard",
    "CDX": "Codex entries", "FSD": "FSS signals", "NAV": "Nav route", "APS": "Approach settlement",
    "SBC": "Barycentre scans", "FSB": "FSS body signals", "DCK": "Docking", "SCO": "Scan organic",
    "FCJ": "Fleet carrier materials (journal)", "FCC": "Fleet carrier materials (CAPI)",
    "BLK": "Black market (retired schema)", "JNL": "Journal",
}


def technical_context(settings: Settings) -> dict:
    """Every rule in config/rules.yaml, exactly as configured, plus the scoring parameters."""
    scoring = settings.load_yaml("scoring.yaml")
    rules_cfg = settings.load_yaml("rules.yaml").get("rules", {})
    sev_w = scoring["severity_weights"]
    families: dict[str, list[dict]] = {}
    off = []
    for rule_id, cfg in sorted(rules_cfg.items()):
        schemas = cfg.get("schemas", "*")
        row = {"id": rule_id, "severity": cfg["severity"], "weight": sev_w.get(cfg["severity"], 0),
               "schemas": "all" if schemas == "*" else ", ".join(schemas),
               "title": cfg.get("title", ""), "source": cfg.get("source", "")}
        if not cfg.get("enabled", True):
            off.append({**row, "why": SWITCHED_OFF.get(rule_id, "")})
            continue
        families.setdefault(RULE_FAMILIES.get(rule_id.split("-")[0], "Other"), []).append(row)
    order = list(RULE_FAMILIES.values()) + ["Other"]
    groups = [(name, families[name]) for name in order if name in families]
    return {"groups": groups, "off": off, "total": sum(len(r) for _, r in groups),
            "scoring": scoring, "severity_weights": sev_w}
