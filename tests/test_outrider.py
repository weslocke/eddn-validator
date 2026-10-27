import datetime as dt

from eddn_validator.outrider import day_bounds, group_commanders, position

UTC = dt.timezone.utc


def point(t, uploader, xyz, name="Sys"):
    return (float(t), uploader, xyz, name, dt.datetime.fromtimestamp(t, UTC))


def test_position_from_starpos_and_route():
    assert position({"StarPos": [1, 2.5, -3], "StarSystem": "A"}) == (1.0, 2.5, -3.0, "A")
    route = {"Route": [{"StarSystem": "B", "StarPos": [4, 5, 6]}]}
    assert position(route) == (4.0, 5.0, 6.0, "B")
    assert position({"StarPos": [1, 2]}) is None
    assert position({"StarPos": [True, 2, 3]}) is None
    assert position("nope") is None


def test_close_positions_are_one_commander():
    groups = group_commanders([point(0, "a", (0, 0, 0)), point(5000, "b", (100, 100, 100))])
    assert len(groups) == 1 and groups[0]["messages"] == 2


def test_far_apart_are_two_commanders():
    groups = group_commanders([point(0, "a", (0, 0, 0)), point(10, "b", (5000, 0, 0))])
    assert len(groups) == 2


def test_neutron_jump_links_only_when_close_in_time():
    near_in_time = group_commanders([point(0, "a", (0, 0, 0)), point(300, "b", (600, 0, 0))])
    far_in_time = group_commanders([point(0, "a", (0, 0, 0)), point(3000, "b", (600, 0, 0))])
    assert len(near_in_time) == 1 and len(far_in_time) == 2


def test_shared_hash_links_distant_positions():
    groups = group_commanders([point(0, "same", (0, 0, 0)), point(60, "same", (9000, 0, 0))])
    assert len(groups) == 1


def test_group_reports_latest_location_and_busiest_first():
    groups = group_commanders([
        point(0, "a", (0, 0, 0), "Start"), point(100, "a", (10, 0, 0), "Later"),
        point(50, "z", (9000, 0, 0), "Elsewhere"),
    ])
    assert [g["where"] for g in groups] == ["Later", "Elsewhere"]


def test_day_bounds_follow_new_york_dst():
    summer = day_bounds(dt.date(2026, 7, 1))
    winter = day_bounds(dt.date(2026, 12, 1))
    assert summer[0] == dt.datetime(2026, 7, 1, 4, tzinfo=UTC)
    assert winter[0] == dt.datetime(2026, 12, 1, 5, tzinfo=UTC)
    # The autumn change day is 25 hours long.
    start, end = day_bounds(dt.date(2026, 11, 1))
    assert end - start == dt.timedelta(hours=25)


def test_message_key_matches_relay_number_formatting():
    from eddn_validator.mark_mine import message_key
    sent = {"timestamp": "t", "StarPos": [3324.0, -1.5, 0.0], "b": 1, "a": 2}
    relayed = {"a": 2, "b": 1, "StarPos": [3324, -1.5, 0], "timestamp": "t"}
    assert message_key("ref", sent) == message_key("ref", relayed)
    assert message_key("ref", sent) != message_key("ref/test", relayed)


def _every_pair(points):
    """The grouping by comparing every pair of points: the rule group_commanders must match."""
    from eddn_validator.outrider import LINK_LY, NEAR_LY, NEAR_S
    parent = list(range(len(points)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i, a in enumerate(points):
        for j in range(i + 1, len(points)):
            b = points[j]
            d2 = sum((p - q) ** 2 for p, q in zip(a[2], b[2]))
            if (a[1] is not None and a[1] == b[1]) or d2 <= LINK_LY ** 2 or (
                    abs(a[0] - b[0]) <= NEAR_S and d2 <= NEAR_LY ** 2):
                parent[root(i)] = root(j)
    sizes = {}
    for i in range(len(points)):
        sizes[root(i)] = sizes.get(root(i), 0) + 1
    return sorted(sizes.values())


def test_grouping_matches_every_pair_comparison():
    import random
    rng = random.Random(7)
    for _ in range(40):
        # A few commanders' worth of systems, each reported many times, some near each other.
        systems = [tuple(rng.choice((0, 500, 650, 2000)) + rng.uniform(-400, 400) for _ in range(3))
                   for _ in range(rng.randint(1, 25))]
        points = [point(rng.uniform(0, 86400), rng.choice((None, None, "h1", "h2", "h3")), rng.choice(systems))
                  for _ in range(rng.randint(1, 120))]
        assert sorted(g["messages"] for g in group_commanders(points)) == _every_pair(points)
