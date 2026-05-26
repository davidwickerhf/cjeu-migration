"""Top-level orchestration: scrape every window, consolidate, push.

Designed for long-running, possibly-interrupted execution on Vast.ai:

- Each window is processed independently; one window's failure never blocks
  the others.
- State is persisted to ``manifest.json`` after every transition, so the
  process can be killed and restarted at any time.
- The scrape phase is idempotent — completed windows are skipped on restart;
  failed windows are retried up to ``max_window_retries``; only ``exhausted``
  windows require operator attention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from cjeu_migration.config import Config
from cjeu_migration.consolidate import (
    consolidate_cases,
    consolidate_fulltexts,
    copy_fields_md,
    write_dataset_card,
)
from cjeu_migration.huggingface_push import push_dataset
from cjeu_migration.manifest import Manifest, WindowStatus
from cjeu_migration.scraper import ScrapeError, scrape_window
from cjeu_migration.windowing import Window, iter_windows


log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    windows_total: int
    windows_completed: int
    windows_failed: int
    windows_exhausted: int
    cases_rows: int
    fulltexts_rows: int
    uploaded: List[str]


def run(
    config: Config,
    *,
    scrape_fn: Optional[Callable] = None,
    push_fn: Optional[Callable] = None,
    consolidate_only: bool = False,
) -> RunSummary:
    """Run the full pipeline. Returns a :class:`RunSummary`."""
    log.info(
        "starting CJEU migration: %s -> %s, window=%s",
        config.start_date.isoformat(),
        config.end_date.isoformat(),
        config.window,
    )

    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest.load(config.manifest_path)

    windows = list(
        iter_windows(config.start_date, config.end_date, kind=config.window)
    )
    for window in windows:
        manifest.register(window.window_id, window.sd.isoformat(), window.ed.isoformat())

    if not consolidate_only:
        _scrape_pending(config, manifest, windows, scrape_fn=scrape_fn)

    cases_df, fulltexts_df = _consolidate(config)

    uploaded: List[str] = []
    if not config.skip_upload:
        uploaded = _push(config, cases_df, fulltexts_df, push_fn=push_fn)
    else:
        log.info("SKIP_UPLOAD set — leaving dataset at %s", config.consolidated_dir)

    summary_counts = manifest.summary()
    return RunSummary(
        windows_total=len(windows),
        windows_completed=summary_counts.get(WindowStatus.COMPLETED.value, 0),
        windows_failed=summary_counts.get(WindowStatus.FAILED.value, 0),
        windows_exhausted=summary_counts.get(WindowStatus.EXHAUSTED.value, 0),
        cases_rows=len(cases_df),
        fulltexts_rows=len(fulltexts_df),
        uploaded=uploaded,
    )


def _scrape_pending(
    config: Config,
    manifest: Manifest,
    windows: List[Window],
    *,
    scrape_fn: Optional[Callable] = None,
) -> None:
    """Process every not-yet-done window. Failures never abort the loop."""
    scrape = scrape_fn or scrape_window
    for window in windows:
        if not manifest.should_process(window.window_id, config.max_window_retries):
            state = manifest.get(window.window_id)
            if state is not None:
                log.info(
                    "skipping window %s (status=%s, attempts=%d)",
                    window.window_id, state.status.value, state.attempts,
                )
            continue

        manifest.mark_in_progress(window.window_id)
        try:
            result = scrape(
                window,
                cases_dir=config.cases_dir,
                fulltexts_dir=config.fulltexts_dir,
                threads=config.extractor_threads,
                max_ecli=config.max_ecli_per_window,
                max_attempts=config.max_window_retries,
            )
        except ScrapeError as exc:
            log.warning("window %s failed: %s", window.window_id, exc)
            manifest.mark_failed(window.window_id, str(exc), config.max_window_retries)
            continue
        except Exception as exc:  # noqa: BLE001 — unknown failure modes from extractor
            log.exception("unexpected error scraping window %s", window.window_id)
            manifest.mark_failed(window.window_id, repr(exc), config.max_window_retries)
            continue

        manifest.mark_completed(
            window.window_id,
            row_count=result.row_count,
            fulltext_count=result.fulltext_count,
        )

    summary = manifest.summary()
    log.info(
        "scrape phase done: completed=%d failed=%d exhausted=%d pending=%d in_progress=%d",
        summary["completed"], summary["failed"], summary["exhausted"],
        summary["pending"], summary["in_progress"],
    )


def _consolidate(config: Config):
    """Run consolidate phase, return (cases_df, fulltexts_df)."""
    config.consolidated_dir.mkdir(parents=True, exist_ok=True)
    cases_df = consolidate_cases(
        config.cases_dir, config.consolidated_dir / "cases.parquet"
    )
    fulltexts_df = consolidate_fulltexts(
        config.fulltexts_dir, config.consolidated_dir / "fulltexts.parquet"
    )

    # Build the schema lists for the dataset card.
    canonical_columns = _canonical_columns()
    discovered_columns = sorted(
        c for c in cases_df.columns
        if c not in canonical_columns and not c.startswith("__")
    )
    write_dataset_card(
        config.consolidated_dir / "README.md",
        cases_rows=len(cases_df),
        fulltexts_rows=len(fulltexts_df),
        start_date=config.start_date.isoformat(),
        end_date=config.end_date.isoformat(),
        canonical_columns=canonical_columns,
        discovered_columns=discovered_columns,
        hf_dataset_repo=config.hf_dataset_repo,
    )
    copy_fields_md(config.consolidated_dir / "FIELDS.md")
    return cases_df, fulltexts_df


def _canonical_columns() -> List[str]:
    """Pull the canonical column list from the installed cellar-extractor."""
    try:
        from cellar_extractor import schema  # type: ignore

        return list(schema.CANONICAL_COLUMNS)
    except Exception:  # noqa: BLE001
        return []


def _push(
    config: Config,
    cases_df,
    fulltexts_df,
    *,
    push_fn: Optional[Callable] = None,
) -> List[str]:
    push = push_fn or push_dataset
    if config.hf_token is None:
        log.warning(
            "HUGGINGFACE_TOKEN not set — skipping upload. Set the token "
            "or pass --skip-upload to silence this warning.",
        )
        return []
    commit = (
        f"Refresh CJEU corpus covering "
        f"{config.start_date.isoformat()}..{config.end_date.isoformat()}: "
        f"{len(cases_df)} cases, {len(fulltexts_df)} fulltexts"
    )
    return push(
        config.consolidated_dir,
        config.hf_dataset_repo,
        token=config.hf_token,
        commit_message=commit,
    )
