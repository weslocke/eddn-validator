from datetime import date

from eddn_validator.partitions import days_to_cover, partition_name


def test_days_to_cover_default_is_today_plus_ahead():
    assert days_to_cover(date(2026, 10, 13), 2) == [
        date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)]


def test_days_to_cover_back_dated_is_contiguous():
    days = days_to_cover(date(2026, 10, 13), 7, since=date(2026, 10, 8))
    assert days[0] == date(2026, 10, 8) and days[-1] == date(2026, 10, 20)
    assert len(days) == 13 and len(set(days)) == 13


def test_days_to_cover_ignores_future_since():
    assert days_to_cover(date(2026, 10, 13), 0, since=date(2026, 12, 1)) == [date(2026, 10, 13)]


def test_partition_name():
    assert partition_name(date(2026, 1, 2)) == "messages_p20260102"
