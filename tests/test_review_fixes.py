"""Regression tests for the bugs found by the 2026-10-14 multi-agent code review."""
import bz2
import io

from eddn_validator import facts, import_archive, pairing
from eddn_validator.classify import classify, parse_ts
from eddn_validator.profiles import Acc
from eddn_validator.rules import Ctx, starpos_outside_system_address


def test_parse_ts_out_of_range_offset_is_none_not_overflow():
    assert parse_ts("0001-01-01T00:00:00+01:00") is None
    assert parse_ts("9999-12-31T23:59:59-01:00") is None


def test_header_strings_are_column_safe():
    c = classify({"$schemaRef": "x", "header": {"softwareName": "Bad\ud800App\x00",
                                                "softwareVersion": "1\ud800"}, "message": {}})
    c.software_name.encode("utf-8")   # would raise on a lone surrogate
    assert "\x00" not in c.software_name


def test_same_and_positions_survive_huge_integers():
    assert facts.same(10**400, 10**400) and not facts.same(10**400, 1.5)
    assert facts.valid_pos([10**400, 0, 0]) is None
    assert facts.valid_pos([float("nan"), 0, 0]) is None
    assert facts.systems_in({"StarSystem": "X", "SystemAddress": 5, "StarPos": [10**400, 0, 0]}) == []


def test_pairing_ignores_unhashable_ids():
    assert pairing.compare_body({"SystemAddress": 1, "BodyID": [1, 2]}, {}) is None
    assert pairing.compare_body({"SystemAddress": {"a": 1}, "BodyID": 3}, {}) is None


def test_gen030_reports_garbage_position_instead_of_crashing():
    ctx = Ctx({"$schemaRef": "https://eddn.edcd.io/schemas/journal/1", "header": {},
               "message": {"StarSystem": "Sol", "SystemAddress": 10477373803, "StarPos": [10**400, 0, 0]}})
    assert "not a position" in starpos_outside_system_address(ctx)


def test_profile_accumulator_skips_huge_numbers():
    acc = Acc()
    acc.add({"MassEM": 10**400, "Radius": 5.0})
    assert acc.profile()["paths"]["Radius"]["num"]["max"] == 5.0


def test_stream_lines_handles_stream_boundary_on_read_boundary(monkeypatch):
    first = bz2.compress(b"a\nb\n")
    second = bz2.compress(b"c\nd\n")

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(b"")
            self.parts = [first, second]   # each read returns exactly one whole stream

        def read(self, n=-1):
            return self.parts.pop(0) if self.parts else b""

        def getheader(self, name, default=None):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(import_archive.urllib.request, "urlopen", lambda *a, **k: Response())
    assert list(import_archive.stream_lines("http://x")) == [b"a", b"b", b"c", b"d"]


def test_reference_files_refill_newest_first():
    from datetime import date
    files = [("u1", "A", date(2026, 4, 17)), ("u2", "A", date(2026, 10, 13))]
    assert [f[0] for f in import_archive.newest_first(files)] == ["u2", "u1"]


class _Body(io.BytesIO):
    def __init__(self, data, length):
        super().__init__(data)
        self.length_header = length

    def getheader(self, name, default=None):
        return self.length_header

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_cut_off_download_is_an_error_not_a_short_file(monkeypatch):
    import pytest
    whole = bz2.compress(b"".join(b"line %d\n" % i for i in range(5000)))
    # The server closes early: fewer bytes than announced.
    monkeypatch.setattr(import_archive.urllib.request, "urlopen",
                        lambda *a, **k: _Body(whole[:len(whole) // 2], str(len(whole))))
    with pytest.raises(import_archive.IncompleteDownload):
        list(import_archive.stream_lines("http://x"))
    # No length announced (chunked): the cut-off bzip2 stream gives it away.
    monkeypatch.setattr(import_archive.urllib.request, "urlopen",
                        lambda *a, **k: _Body(whole[:len(whole) // 2], None))
    with pytest.raises(import_archive.IncompleteDownload):
        list(import_archive.stream_lines("http://x"))
    monkeypatch.setattr(import_archive.urllib.request, "urlopen",
                        lambda *a, **k: _Body(whole, str(len(whole))))
    assert len(list(import_archive.stream_lines("http://x"))) == 5000


def test_reference_pass_refuses_partial_reimport():
    import pytest
    from datetime import date
    with pytest.raises(SystemExit):
        import_archive.import_archive(None, date(2026, 10, 1), date(2026, 10, 2),
                                      categories={"Journal.Scan"}, reference=True)
    with pytest.raises(SystemExit):
        import_archive.import_archive(None, date(2026, 10, 1), date(2026, 10, 2),
                                      include_test=False, reference=True)


def test_hostile_digit_runs_and_failing_checks_do_not_crash_scoring():
    from eddn_validator.classify import game_branch
    from eddn_validator.config import Settings
    from eddn_validator.id64 import name_matches_address
    from eddn_validator.rules import evaluate, load_rules
    huge = "Byeia Eur BH-C d" + "9" * 5000 + "-1"
    assert name_matches_address(huge, 12345) is None
    assert game_branch("9" * 5000 + ".1") == "unknown"
    rules = load_rules(Settings.from_env().load_yaml("rules.yaml"))
    env = {"$schemaRef": "https://eddn.edcd.io/schemas/journal/1",
           "header": {"softwareName": "x", "softwareVersion": "1", "gameversion": "4.0", "gamebuild": "r1"},
           "message": {"event": "FSDJump", "StarSystem": huge, "SystemAddress": 1,
                       "StarPos": [0, 0, 0], "timestamp": "2026-10-01T00:00:00Z"}}
    evaluate(rules, env)    # must not raise


def test_gen035_needs_a_word_boundary():
    from eddn_validator.config import Settings
    from eddn_validator.rules import evaluate, load_rules
    rules = [r for r in load_rules(Settings.from_env().load_yaml("rules.yaml")) if r.id == "GEN-035"]
    def env(system, body):
        return {"$schemaRef": "https://eddn.edcd.io/schemas/journal/1",
                "header": {"softwareName": "x", "softwareVersion": "1"},
                "message": {"event": "Scan", "StarSystem": system, "BodyName": body}}
    assert evaluate(rules, env("Byeia Eur BH-C d13-1", "Byeia Eur BH-C d13-10 2"))
    assert not evaluate(rules, env("Byeia Eur BH-C d13-1", "Byeia Eur BH-C d13-1 A 2"))
    assert not evaluate(rules, env("Byeia Eur BH-C d13-1", "Byeia Eur BH-C d13-1"))


def test_size_guard_triggers_on_free_space_not_database_size(monkeypatch):
    from eddn_validator import retention
    monkeypatch.setattr(retention, "database_size", lambda conn: 900 * retention.GIB)
    monkeypatch.setattr(retention, "import_running", lambda conn: False)
    monkeypatch.setattr(retention, "disk_free_gib", lambda path: 200.0)
    # Plenty of free space and no database cap: nothing to do, whatever the database size.
    assert retention.size_guard(None, {"min_free_gb": 60, "disk_probe": "/x", "size_cap_gb": None}) == []
    monkeypatch.setattr(retention, "disk_free_gib", lambda path: 40.0)
    monkeypatch.setattr(retention.partitions, "existing_partitions", lambda conn: {})
    assert retention.size_guard(None, {"min_free_gb": 60, "disk_probe": "/x"}) == []  # nothing old to trim


def test_missing_facts_keeps_only_missing_places(monkeypatch):
    from datetime import datetime, timezone
    from eddn_validator import facts, missing_facts
    stored = []
    monkeypatch.setattr(facts, "store", lambda conn, batch: stored.append(batch))

    class Conn:
        def transaction(self):
            import contextlib
            return contextlib.nullcontext()

    import threading
    missing_facts._init(None, ({1}, {(1, 2)}, {3_700_000_123}), threading.Lock())
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    b = facts.Batch()
    b.add("journal/1:Scan", {"SystemAddress": 1, "BodyID": 2, "StarSystem": "Sol", "StarPos": [0, 0, 0],
                             "BodyName": "Sol 1", "Radius": 1.0}, "live", "journal", "EDMC", t, None)
    b.add("journal/1:Scan", {"SystemAddress": 9, "BodyID": 2, "StarSystem": "Achenar", "StarPos": [67.5, -119.5, 24.8],
                             "BodyName": "Achenar 1"}, "live", "journal", "EDMC", t, None)
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    n = missing_facts._flush(Conn(), b, now)
    batch = stored[0]
    assert set(batch.systems) == {1} and set(batch.bodies) == {(1, 2)} and n == 2
    assert batch.systems[1]["last"] == now and batch.bodies[(1, 2)]["last"] == now


class _GuardConn:
    """Enough of a psycopg connection for size_guard: DELETE trims rows, VACUUM FULL is busy."""
    def __init__(self):
        self.deleted, self.vacuums = [], 0

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()

    def execute(self, query, params=None):
        import psycopg
        text = query if isinstance(query, str) else query.as_string(None)
        class R:
            rowcount = 0
            def fetchone(self):
                return None
        r = R()
        if text.startswith("DELETE"):
            self.deleted.append(text); r.rowcount = 1000
        elif text.startswith("VACUUM"):
            self.vacuums += 1
            raise psycopg.errors.LockNotAvailable("busy")
        return r


def test_size_guard_stops_when_compaction_is_postponed(monkeypatch):
    from datetime import date
    from eddn_validator import retention
    monkeypatch.setattr(retention, "import_running", lambda conn: False)
    monkeypatch.setattr(retention, "disk_free_gib", lambda path: 40.0)       # stays low: nothing frees
    monkeypatch.setattr(retention.partitions, "existing_partitions",
                        lambda conn: {f"messages_p202605{d:02d}": date(2026, 5, d) for d in range(1, 11)})
    conn = _GuardConn()
    trimmed = retention.size_guard(conn, {"min_free_gb": 60, "disk_probe": "/x"})
    assert len(conn.deleted) == 1 and conn.vacuums == 1 and len(trimmed) == 1


def test_size_guard_waits_for_imports_above_the_hard_floor(monkeypatch):
    from eddn_validator import retention
    monkeypatch.setattr(retention, "import_running", lambda conn: True)
    monkeypatch.setattr(retention, "disk_free_gib", lambda path: 55.0)
    conn = _GuardConn()
    assert retention.size_guard(conn, {"min_free_gb": 60, "hard_min_free_gb": 52, "disk_probe": "/x"}) == []
    assert conn.deleted == []


def test_conflict_heals_only_with_two_apps_or_the_held_value():
    from datetime import datetime, timezone
    from eddn_validator import facts
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    existing = (5, "Old Name", [1.0, 2.0, 3.0], 10, ["EDMC"], True, t)

    class Conn:
        def __init__(self):
            self.rows = None
        def execute(self, q, p=None):
            class R:
                def fetchall(_):
                    return [existing] if "FROM facts_system" in q else []
            return R()
        def cursor(self):
            conn = self
            class C:
                def __enter__(s): return s
                def __exit__(s, *a): return False
                def executemany(s, q, rows):
                    if "facts_system" in q:
                        conn.rows = rows
            return C()

    def run(name, pos, groups):
        b = facts.Batch()
        b.systems[5] = {"names": {name}, "positions": [pos], "groups": set(groups), "n": 3, "last": t}
        c = Conn(); facts.store(c, b); return c.rows[0]
    assert run("New Name", (9.0, 9.0, 9.0), ["EDMC"])[5] is True              # one app, new value: still disputed
    assert run("New Name", (9.0, 9.0, 9.0), ["EDMC", "EDDiscovery"])[5] is False  # two apps agree: healed
    assert run("Old Name", (1.0, 2.0, 3.0), ["EDMC"])[5] is False             # confirms the held value: healed
