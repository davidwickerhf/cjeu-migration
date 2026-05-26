"""Windowing iteration tests."""

from datetime import date

import pytest

from cjeu_migration.windowing import iter_windows


def test_month_windows_for_one_year():
    windows = list(iter_windows(date(2020, 1, 1), date(2020, 12, 31), kind="month"))
    assert len(windows) == 12
    assert windows[0].window_id == "2020-01"
    assert windows[0].sd == date(2020, 1, 1)
    assert windows[0].ed == date(2020, 1, 31)
    assert windows[11].window_id == "2020-12"
    assert windows[11].ed == date(2020, 12, 31)


def test_month_windows_clip_partial_start_and_end():
    """Range starting mid-month clips the first window, ending mid-month clips the last."""
    windows = list(iter_windows(date(2020, 1, 15), date(2020, 3, 10), kind="month"))
    assert [w.window_id for w in windows] == ["2020-01", "2020-02", "2020-03"]
    assert windows[0].sd == date(2020, 1, 15)
    assert windows[0].ed == date(2020, 1, 31)
    assert windows[1].sd == date(2020, 2, 1)
    assert windows[1].ed == date(2020, 2, 29)  # leap year
    assert windows[2].sd == date(2020, 3, 1)
    assert windows[2].ed == date(2020, 3, 10)


def test_month_windows_leap_year_february():
    windows = list(iter_windows(date(2020, 2, 1), date(2020, 2, 29), kind="month"))
    assert windows[0].ed == date(2020, 2, 29)

    windows = list(iter_windows(date(2021, 2, 1), date(2021, 2, 28), kind="month"))
    assert windows[0].ed == date(2021, 2, 28)


def test_quarter_windows():
    windows = list(iter_windows(date(2020, 1, 1), date(2020, 12, 31), kind="quarter"))
    assert [w.window_id for w in windows] == [
        "2020-Q1",
        "2020-Q2",
        "2020-Q3",
        "2020-Q4",
    ]
    assert windows[0].sd == date(2020, 1, 1)
    assert windows[0].ed == date(2020, 3, 31)
    assert windows[3].sd == date(2020, 10, 1)
    assert windows[3].ed == date(2020, 12, 31)


def test_quarter_window_starting_mid_quarter():
    """Starting in February should still produce Q1 window starting from sd."""
    windows = list(iter_windows(date(2020, 2, 15), date(2020, 4, 30), kind="quarter"))
    assert windows[0].window_id == "2020-Q1"
    assert windows[0].sd == date(2020, 2, 15)
    assert windows[0].ed == date(2020, 3, 31)
    assert windows[1].window_id == "2020-Q2"
    assert windows[1].sd == date(2020, 4, 1)
    assert windows[1].ed == date(2020, 4, 30)


def test_year_windows():
    windows = list(iter_windows(date(2018, 6, 1), date(2020, 6, 30), kind="year"))
    assert [w.window_id for w in windows] == ["2018", "2019", "2020"]
    assert windows[0].sd == date(2018, 6, 1)
    assert windows[0].ed == date(2018, 12, 31)
    assert windows[1].sd == date(2019, 1, 1)
    assert windows[1].ed == date(2019, 12, 31)
    assert windows[2].sd == date(2020, 1, 1)
    assert windows[2].ed == date(2020, 6, 30)


def test_empty_range_yields_no_windows():
    windows = list(iter_windows(date(2020, 5, 1), date(2020, 4, 30), kind="month"))
    assert windows == []


def test_single_day_window():
    """sd == ed = one window. The Window object preserves both."""
    windows = list(iter_windows(date(2020, 6, 15), date(2020, 6, 15), kind="month"))
    assert len(windows) == 1
    assert windows[0].sd == date(2020, 6, 15)
    assert windows[0].ed == date(2020, 6, 15)


def test_iso_strings_include_end_of_day_for_ed():
    """ed_iso should carry T23:59:59 so date-only documents at the boundary
    are included by the cellar-extractor filter."""
    windows = list(iter_windows(date(2020, 1, 1), date(2020, 1, 31), kind="month"))
    assert windows[0].sd_iso == "2020-01-01"
    assert windows[0].ed_iso == "2020-01-31T23:59:59"


def test_full_corpus_window_count_sanity():
    """1954 → 2024 in months should produce 71 × 12 = 852 windows."""
    windows = list(iter_windows(date(1954, 1, 1), date(2024, 12, 31), kind="month"))
    assert len(windows) == 71 * 12


def test_invalid_kind_raises():
    with pytest.raises(ValueError):
        list(iter_windows(date(2020, 1, 1), date(2020, 12, 31), kind="weekly"))  # type: ignore[arg-type]
