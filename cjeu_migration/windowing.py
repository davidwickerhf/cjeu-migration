"""Date-range windowing for the corpus extraction loop.

Splits a full date range (default 1954-01-01 → today) into bite-sized windows
the cellar-extractor can process in a single ``get_cellar_extra`` call. Window
ids are sortable strings so the manifest stays human-readable.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator, Literal


WindowKind = Literal["month", "quarter", "year"]


@dataclass(frozen=True)
class Window:
    window_id: str            # e.g. "2020-01", "2020-Q1", "2020"
    sd: date                  # inclusive start
    ed: date                  # inclusive end

    @property
    def sd_iso(self) -> str:
        return self.sd.isoformat()

    @property
    def ed_iso(self) -> str:
        # End at 23:59:59 so date-range filters that match dates also match
        # the same day's timestamped documents.
        return f"{self.ed.isoformat()}T23:59:59"


def _end_of_month(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def _next_month_start(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _next_quarter_start(d: date) -> date:
    q_start_month = ((d.month - 1) // 3) * 3 + 1
    next_q_month = q_start_month + 3
    if next_q_month > 12:
        return date(d.year + 1, next_q_month - 12, 1)
    return date(d.year, next_q_month, 1)


def _quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def iter_windows(
    start_date: date, end_date: date, kind: WindowKind = "month"
) -> Iterator[Window]:
    """Yield :class:`Window` objects covering ``[start_date, end_date]`` inclusive.

    The last window is clipped at ``end_date``. If ``end_date < start_date``
    the iterator is empty.
    """
    if end_date < start_date:
        return

    if kind == "month":
        cursor = date(start_date.year, start_date.month, 1)
        while cursor <= end_date:
            month_end = _end_of_month(cursor)
            window_sd = max(cursor, start_date)
            window_ed = min(month_end, end_date)
            yield Window(
                window_id=f"{cursor.year:04d}-{cursor.month:02d}",
                sd=window_sd,
                ed=window_ed,
            )
            cursor = _next_month_start(cursor)
    elif kind == "quarter":
        q_start_month = ((start_date.month - 1) // 3) * 3 + 1
        cursor = date(start_date.year, q_start_month, 1)
        while cursor <= end_date:
            next_start = _next_quarter_start(cursor)
            q_end = next_start - timedelta(days=1)
            window_sd = max(cursor, start_date)
            window_ed = min(q_end, end_date)
            yield Window(
                window_id=f"{cursor.year:04d}-Q{_quarter_of(cursor)}",
                sd=window_sd,
                ed=window_ed,
            )
            cursor = next_start
    elif kind == "year":
        cursor = date(start_date.year, 1, 1)
        while cursor <= end_date:
            year_end = date(cursor.year, 12, 31)
            window_sd = max(cursor, start_date)
            window_ed = min(year_end, end_date)
            yield Window(
                window_id=f"{cursor.year:04d}",
                sd=window_sd,
                ed=window_ed,
            )
            cursor = date(cursor.year + 1, 1, 1)
    else:
        raise ValueError(f"Unknown window kind: {kind!r}")
