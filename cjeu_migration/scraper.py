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
    verify_cellar: bool = False,
    manifestations_fn: Optional[Callable[[str], list]] = None,
    refetch_fn: Optional[Callable[[str], Any]] = None,
) -> ScrapeResult:
    """Scrape one window, returning where the outputs landed.

    Output layout::

        cases_dir/<window_id>.csv
        fulltexts_dir/<window_id>.json

    With ``verify_cellar``, sector-6 ECLIs that came back without any CELLAR
    text are re-checked against CELLAR (see
    :func:`repair_missing_cellar_texts`).

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

    if verify_cellar:
        try:
            repaired = repair_missing_cellar_texts(
                fulltexts_path,
                cases_path=cases_path,
                manifestations_fn=manifestations_fn,
                refetch_fn=refetch_fn,
                max_attempts=max_attempts,
            )
        except CellarCoverageError as exc:
            raise ScrapeError(f"window {window.window_id}: {exc}") from exc
        if repaired:
            log.info(
                "window %s: recovered dropped texts for %d ECLIs",
                window.window_id, repaired,
            )

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


class CellarCoverageError(RuntimeError):
    """CELLAR has manifestations for a document but no text could be fetched."""


def _default_manifestations_fn(celex: str) -> list:
    from cellar_extractor import get_cellar_manifestations_by_celex  # type: ignore

    _, manifestations = get_cellar_manifestations_by_celex(celex, sector="6")
    return manifestations


def _default_refetch_fn(
    celex: str, document_id: Optional[str] = None, use_cellar: bool = True
) -> Any:
    from cellar_extractor import eurlex_scraping  # type: ignore

    # ``get_case_data_by_celex_id`` is lru-cached and swallows CELLAR and
    # InfoCuria errors, so a transient failure would otherwise be replayed
    # from the cache.
    eurlex_scraping._get_case_data_cached.cache_clear()
    return eurlex_scraping.get_case_data_by_celex_id(
        celex, document_id=document_id, use_cellar=use_cellar
    )


def _read_identities(cases_path: Optional[Path]) -> dict[str, dict]:
    """Per-ECLI identity columns written by cellar-extractor's catalogue step."""
    if cases_path is None or not cases_path.exists():
        return {}
    import csv
    import sys

    csv.field_size_limit(sys.maxsize)
    identities = {}
    with cases_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ecli = (row.get("ecli") or "").strip()
            if ecli:
                identities[ecli] = {
                    "identity_source": (row.get("identity_source") or "").strip(),
                    "document_id": (row.get("infocuria_document_id") or "").strip(),
                    "lookup_celex": (
                        (row.get("celex") or "").strip()
                        or (row.get("infocuria_celex") or "").split(";", 1)[0].strip()
                    ),
                }
    return identities


def _has_text(rows: list) -> bool:
    return any(str(row.get("text") or "").strip() for row in rows)


def _build_records(data: Any, celex: str, ecli: str, missing_reasons: str) -> list:
    from cellar_extractor.fulltext_saving import _build_fulltext_records  # type: ignore

    return _build_fulltext_records(data, celex, ecli, missing_reasons)


def _has_cellar_text(rows: list) -> bool:
    return any(
        row.get("text_source") == "CELLAR_ITEM" and str(row.get("text") or "").strip()
        for row in rows
    )


def repair_missing_cellar_texts(
    fulltexts_path: Path,
    *,
    cases_path: Optional[Path] = None,
    manifestations_fn: Optional[Callable[[str], list]] = None,
    refetch_fn: Optional[Callable[[str], Any]] = None,
    build_records_fn: Optional[Callable[..., list]] = None,
    max_attempts: int = 3,
) -> int:
    """Re-fetch sector-6 documents whose CELLAR texts were silently dropped.

    ``cellar-extractor`` treats any CELLAR failure during the language merge
    as "no CELLAR text" and keeps whatever InfoCuria returned (possibly
    nothing). For every sector-6 ECLI in the window with no CELLAR text, ask
    CELLAR whether manifestations exist; if they do, re-fetch the document
    and replace its rows. Returns the number of ECLIs repaired and raises
    :class:`CellarCoverageError` if CELLAR still yields no text, so the
    window is failed and retried instead of being published incomplete.

    Documents known only to InfoCuria (``identity_source == "infocuria"`` in
    the window's cases CSV) carry at best InfoCuria's procedure-level CELEX,
    so CELLAR is never consulted for them; if they came back without any
    text they are re-fetched by InfoCuria document id instead. Some have no
    HTML at all, so those never fail the window.
    """
    if not fulltexts_path.exists():
        return 0
    manifestations_fn = manifestations_fn or _default_manifestations_fn
    refetch_fn = refetch_fn or _default_refetch_fn
    build_records_fn = build_records_fn or _build_records

    import json

    with fulltexts_path.open("r", encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        return 0

    by_ecli: dict[str, list] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("ecli"):
            by_ecli.setdefault(str(entry["ecli"]), []).append(entry)

    identities = _read_identities(cases_path)
    replacements: dict[str, list] = {}
    unrecovered: list[str] = []
    for ecli, rows in by_ecli.items():
        identity = identities.get(ecli, {})
        celex = str(rows[0].get("celex") or "").strip()
        reasons = rows[0].get("missing_reasons") or ""
        if identity.get("identity_source") == "infocuria":
            lookup = identity.get("lookup_celex") or ""
            if _has_text(rows) or not identity.get("document_id") or not lookup:
                continue
            for _ in range(max_attempts):
                data = refetch_fn(
                    lookup, document_id=identity["document_id"], use_cellar=False
                )
                records = build_records_fn(data, celex, ecli, reasons)
                if _has_text(records):
                    replacements[ecli] = records
                    break
            continue
        if not celex.startswith("6") or _has_cellar_text(rows):
            continue
        # CELLAR text is required when CELLAR has the document; otherwise any
        # text (InfoCuria's) is recovered on a best-effort basis.
        required = bool(manifestations_fn(celex))
        if not required and _has_text(rows):
            continue
        recovered = _has_cellar_text if required else _has_text
        for _ in range(max_attempts):
            data = (
                refetch_fn(celex, document_id=identity["document_id"])
                if identity.get("document_id")
                else refetch_fn(celex)
            )
            records = build_records_fn(data, celex, ecli, reasons)
            if recovered(records):
                replacements[ecli] = records
                break
        else:
            if required:
                unrecovered.append(f"{ecli} ({celex})")

    if unrecovered:
        raise CellarCoverageError(
            f"CELLAR has manifestations but no text was fetched for "
            f"{len(unrecovered)} ECLIs: {', '.join(unrecovered[:10])}"
        )
    if not replacements:
        return 0

    output: list = []
    emitted: set[str] = set()
    for entry in entries:
        ecli = str(entry.get("ecli") or "") if isinstance(entry, dict) else ""
        if ecli in replacements:
            if ecli not in emitted:
                output.extend(replacements[ecli])
                emitted.add(ecli)
            continue
        output.append(entry)
    temporary = fulltexts_path.with_suffix(fulltexts_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)
    temporary.replace(fulltexts_path)
    return len(replacements)


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
