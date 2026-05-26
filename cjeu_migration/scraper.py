"""Per-window scraping orchestration on top of ``cellar-extractor``.

The scraper:

1. Calls ``cellar_extractor.get_cellar_extra`` for a single :class:`Window`.
2. Persists the per-window outputs (CSV + fulltext JSON) into the workspace.
3. Returns a structured :class:`ScrapeResult` for the runner.
4. Wraps the whole call in ``tenacity`` exponential-backoff retries so a
   transient SPARQL outage doesn't kill the window.

The runner remains responsible for manifest transitions — the scraper only
reports facts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from cjeu_migration.windowing import Window


log = logging.getLogger(__name__)


@dataclass
class ScrapeResult:
    window: Window
    row_count: int
    fulltext_count: int
    cases_path: Path
    fulltexts_path: Path
    attempts: int


class ScrapeError(RuntimeError):
    """Raised when a window cannot be scraped after all retries."""


def _default_extra_fn() -> Callable[..., Any]:
    """Lazy import so unit tests can run without cellar-extractor installed."""
    from cellar_extractor import get_cellar_extra  # type: ignore

    return get_cellar_extra


def scrape_window(
    window: Window,
    cases_dir: Path,
    fulltexts_dir: Path,
    *,
    threads: int = 10,
    max_ecli: int = 10_000,
    max_attempts: int = 3,
    extra_fn: Optional[Callable[..., Any]] = None,
) -> ScrapeResult:
    """Scrape one window, returning where the outputs landed.

    Output layout::

        cases_dir/<window_id>.csv
        fulltexts_dir/<window_id>.json

    Raises :class:`ScrapeError` when retries are exhausted.
    """
    cases_dir.mkdir(parents=True, exist_ok=True)
    fulltexts_dir.mkdir(parents=True, exist_ok=True)

    cases_path = cases_dir / f"{window.window_id}.csv"
    fulltexts_path = fulltexts_dir / f"{window.window_id}.json"

    extra_callable = extra_fn or _default_extra_fn()

    attempts_used = 0
    last_exc: Optional[BaseException] = None

    try:
        for attempt in Retrying(
            reraise=True,
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception_type(Exception),
        ):
            with attempt:
                attempts_used = attempt.retry_state.attempt_number
                log.info(
                    "scraping window %s (%s..%s) attempt=%s",
                    window.window_id, window.sd_iso, window.ed_iso, attempts_used,
                )
                _run_extractor(
                    extra_callable,
                    window=window,
                    cases_path=cases_path,
                    fulltexts_path=fulltexts_path,
                    threads=threads,
                    max_ecli=max_ecli,
                )
    except Exception as exc:
        last_exc = exc
        raise ScrapeError(
            f"window {window.window_id} failed after {attempts_used} attempts: {exc}"
        ) from exc

    row_count = _count_csv_rows(cases_path)
    fulltext_count = _count_json_list(fulltexts_path)
    return ScrapeResult(
        window=window,
        row_count=row_count,
        fulltext_count=fulltext_count,
        cases_path=cases_path,
        fulltexts_path=fulltexts_path,
        attempts=attempts_used,
    )


def _run_extractor(
    extra_callable: Callable[..., Any],
    *,
    window: Window,
    cases_path: Path,
    fulltexts_path: Path,
    threads: int,
    max_ecli: int,
) -> None:
    """Call cellar-extractor and write outputs to the window's paths."""
    extra_callable(
        sd=window.sd_iso,
        ed=window.ed_iso,
        max_ecli=max_ecli,
        threads=threads,
        save=True,
        return_data=False,
        metadata_output_path=str(cases_path),
        fulltext_output_path=str(fulltexts_path),
    )


def _count_csv_rows(path: Path) -> int:
    """Count CSV rows excluding the header. Returns 0 for missing files."""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        lines = sum(1 for _ in f)
    return max(0, lines - 1)


def _count_json_list(path: Path) -> int:
    if not path.exists():
        return 0
    import json
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0
    return len(data) if isinstance(data, list) else 0
