from datetime import datetime, timedelta, timezone

from eddn_validator.import_archive import FILE_RE, NAME_RE, Window

T = datetime(2026, 10, 13, 19, 41, tzinfo=timezone.utc)
W = Window(full_start=datetime(2026, 10, 9, 4, 52, tzinfo=timezone.utc),
           full_end=datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc),
           outrider_end=datetime(2026, 10, 13, 18, 8, tzinfo=timezone.utc),
           live_start=T)


def test_window_fills_only_gaps():
    assert W.wanted(datetime(2026, 9, 1, tzinfo=timezone.utc), "EDDI")            # before everything
    assert not W.wanted(datetime(2026, 10, 10, tzinfo=timezone.utc), "EDDI")      # full traffic held
    assert W.wanted(datetime(2026, 10, 12, tzinfo=timezone.utc), "EDDI")          # gap: others missing
    assert not W.wanted(datetime(2026, 10, 12, tzinfo=timezone.utc), "ED Outrider")  # Outrider held
    assert W.wanted(datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc), "ED Outrider")  # nothing ran
    assert not W.wanted(T + timedelta(minutes=1), "EDDI")                          # live listener


def test_empty_database_wants_everything():
    assert Window(None, None, None, None).wanted(T, "EDDI")


def test_listing_and_name_patterns():
    row = '<a href="Journal.Scan-2026-09-01.jsonl.bz2">x</a>'
    m = FILE_RE.search(row)
    assert (m["cat"], m["day"]) == ("Journal.Scan", "2026-09-01")
    line = '{"header": {"softwareName": "E:D Market Connector [Windows]", "x": 1}}'
    assert NAME_RE.search(line).group(1) == "E:D Market Connector [Windows]"
    assert NAME_RE.search('{"softwareName":"Say \\"hi\\""}').group(1) == 'Say \\"hi\\"'
