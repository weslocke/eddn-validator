from eddn_validator.id64 import decode

# (SystemAddress, StarPos) pairs as reported by the reference apps.
KNOWN = [
    (10477373803, (0.0, 0.0, 0.0)),                        # Sol, mass code d
    (2441089347, (-38383.3125, -23.25, 20066.03125)),       # Neamoo HX-L d7-0
    (47102118899, (10737.84375, 375.5, 6458.25)),           # Byeia Eur BH-C d13-1
]


def test_known_systems_inside_their_boxel():
    for address, pos in KNOWN:
        assert decode(address).contains(pos, tolerance=0.0), address


def test_far_position_is_outside():
    boxel = decode(10477373803)
    assert not boxel.contains((500.0, 0.0, 0.0))
    assert boxel.distance_outside((500.0, 0.0, 0.0)) > 400


def test_bad_input():
    assert decode(-1) is None and decode("x") is None and decode(True) is None


def test_procedural_name_matches_address():
    from eddn_validator.id64 import name_matches_address
    assert name_matches_address("Byeia Eur BH-C d13-1", 47102118899) is True
    assert name_matches_address("Neamoo HX-L d7-0", 2441089347) is True
    assert name_matches_address("Byeia Eur BH-C d13-2", 47102118899) is False   # different N2
    assert name_matches_address("Byeia Eur BH-C c13-1", 47102118899) is False   # different mass code
    assert name_matches_address("Sol", 10477373803) is None                      # not procedural
    assert name_matches_address("Col 285 Sector AB-C d1-2", 1) is None           # hand-made sector
    assert name_matches_address("ICZ AB-C d1-2", 1) is None
    assert name_matches_address("Byeia Eur BH-C d13-1", -5) is None
