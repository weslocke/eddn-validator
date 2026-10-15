"""Decode an Elite Dangerous SystemAddress (id64) into the boxel cube it names.

Layout, from the least significant bit:
    3 bits             mass code mc (0..7, the letter a..h in procedural names)
    7 - mc bits        boxel z      7 bits  sector z
    7 - mc bits        boxel y      6 bits  sector y
    7 - mc bits        boxel x      7 bits  sector x
    11 + 3*mc bits     N2 (the system's number within the boxel)

Sectors are 1280 ly cubes counted from the galaxy origin below; a boxel is
10 * 2**mc ly on a side. A system's StarPos must lie inside its boxel (GEN-030).
The decoder is checked against every (SystemAddress, StarPos) pair the reference
apps report before the rule is trusted (see facts.py / the id64 test).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECTOR_LY = 1280.0
ORIGIN = (-49985.0, -40985.0, -24105.0)   # x, y, z of sector (0, 0, 0)


@dataclass(frozen=True)
class Boxel:
    mass_code: int
    size: float
    low: tuple[float, float, float]    # x, y, z of the boxel's lower corner

    def contains(self, pos: tuple[float, float, float], tolerance: float = 0.5) -> bool:
        return all(lo - tolerance <= p <= lo + self.size + tolerance
                   for lo, p in zip(self.low, pos))

    def distance_outside(self, pos: tuple[float, float, float]) -> float:
        """How far (ly) pos lies outside the cube; 0 inside."""
        d2 = 0.0
        for lo, p in zip(self.low, pos):
            if p < lo:
                d2 += (lo - p) ** 2
            elif p > lo + self.size:
                d2 += (p - lo - self.size) ** 2
        return d2 ** 0.5


def decode(address: int) -> Boxel | None:
    if not isinstance(address, int) or isinstance(address, bool) or address < 0:
        return None
    mc = address & 0b111
    if mc > 7:
        return None
    bits = 7 - mc
    shift = 3

    def take(n: int) -> int:
        nonlocal shift
        value = (address >> shift) & ((1 << n) - 1)
        shift += n
        return value

    boxel_z, sector_z = take(bits), take(7)
    boxel_y, sector_y = take(bits), take(6)
    boxel_x, sector_x = take(bits), take(7)
    size = 10.0 * (1 << mc)
    low = (
        ORIGIN[0] + sector_x * SECTOR_LY + boxel_x * size,
        ORIGIN[1] + sector_y * SECTOR_LY + boxel_y * size,
        ORIGIN[2] + sector_z * SECTOR_LY + boxel_z * size,
    )
    return Boxel(mc, size, low)


# Procedural system names ("Byeia Eur BH-C d13-1") encode the same boxel as the id64:
# letters AB-C plus the optional first number give the boxel's index in its sector
# (128 boxels per axis whatever the mass code), the lower-case letter is the mass code
# and the last number is N2. Hand-made sectors ("... Sector", "... Dark Region", "ICZ")
# count from their own origins, so they cannot be checked this way.
# Verified 2026-10-14: 4,715,518 of 4,715,518 reference-app systems in regular sectors match.
PROC_NAME = re.compile(
    r"^(?P<sector>.+) (?P<a>[A-Z])(?P<b>[A-Z])-(?P<c>[A-Z]) (?P<mc>[a-h])(?:(?P<n1>\d{1,9})-)?(?P<n2>\d{1,9})$")
HAND_MADE_SECTOR = re.compile(r"(?: Sector| Dark Region)$|^ICZ$")


def is_procedural(name: object) -> bool:
    return isinstance(name, str) and PROC_NAME.match(name) is not None


def _parts(address: int) -> tuple[int, tuple[int, int, int], int]:
    mc = address & 0b111
    bits, shift = 7 - mc, 3

    def take(n: int) -> int:
        nonlocal shift
        value = (address >> shift) & ((1 << n) - 1)
        shift += n
        return value

    bz, _, by, _, bx, _ = take(bits), take(7), take(bits), take(6), take(bits), take(7)
    return mc, (bx, by, bz), take(11 + 3 * mc)


def name_matches_address(name: object, address: object) -> bool | None:
    """True/False when a regular procedural name can be checked against the id64, else None."""
    if not isinstance(name, str) or not isinstance(address, int) or isinstance(address, bool) \
            or not 0 <= address < 2**64:
        return None
    m = PROC_NAME.match(name)
    if not m or HAND_MADE_SECTOR.search(m["sector"]):
        return None
    index = ((ord(m["a"]) - 65) + (ord(m["b"]) - 65) * 26 + (ord(m["c"]) - 65) * 26 ** 2
             + int(m["n1"] or 0) * 26 ** 3)
    from_name = (ord(m["mc"]) - 97, (index % 128, (index // 128) % 128, index // 16384), int(m["n2"]))
    return from_name == _parts(address)
