"""End-to-end cleanup of a published CJEU HF dataset.

Bundles every safe in-place fix from the post-publish audit into one
idempotent pass:

* **work_cites_work URI → CELEX** (cases.parquet) — SPARQL-resolves the
  raw CELLAR URIs the previous extractor build wrote out, so the citation
  graph is joinable against the rest of the dataset.
* **Drop always-null columns** (cases.parquet) — 11 legislation-only CDM
  predicates that never apply to case law, plus 4 text-describing fields
  that legitimately live in ``fulltexts.parquet``. Zero data loss
  (verified column-by-column).
* **ECLI-level dedup** (both files) — collapses the 14 cases / 14
  fulltexts pairs where the same ECLI was scraped twice through
  overlapping date windows. Only the internal ``__source_window`` column
  differs, so dedup is lossless; the dropped window strings are merged
  back as a ``;``-joined list on the survivor row.
* **Viewer-friendly parquet** (both files) — zstd compression, 2 000-row
  groups for cases / 500-row groups for fulltexts, page-offset indexes
  enabled. Unblocks the HF dataset viewer (300 MB scan cap) and
  ``datasets.load_dataset(streaming=True)``.

Usage::

    # Dry run — fetch + transform + write side-by-side, NO upload.
    DRY_RUN=1 python scripts/cleanup_hf_dataset.py

    # Upload to HF (set HUGGINGFACE_TOKEN in your shell).
    python scripts/cleanup_hf_dataset.py

    # Override target repo (defaults to davidwickerhf/cjeu-opendata).
    HF_DATASET_REPO=org/name python scripts/cleanup_hf_dataset.py

Re-running on an already-cleaned dataset is a no-op: every operation is
idempotent (URIs already in CELEX form pass through; null columns are
detected dynamically; dedup is keyed on ECLI).

Requires ``pip install huggingface_hub pandas pyarrow cellar-extractor``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


log = logging.getLogger("cleanup_hf_dataset")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


# Reuse the URI helpers + parquet writer from the focused fix script so the
# two stay byte-equivalent on the work_cites_work path.
_FOCUSED_SCRIPT = Path(__file__).resolve().parent / "fix_hf_work_cites_work.py"
_spec = importlib.util.spec_from_file_location("fix_hf_work_cites_work", _FOCUSED_SCRIPT)
_focused = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_focused)  # type: ignore

collect_unique_uris = _focused.collect_unique_uris
rewrite_column = _focused.rewrite_column

CASES_ROW_GROUP_SIZE = 2_000
FULLTEXTS_ROW_GROUP_SIZE = 500


# ---------------------------------------------------------------------------
# Pure transforms (unit-testable)
# ---------------------------------------------------------------------------


def drop_always_null_columns(
    df: pd.DataFrame,
    *,
    protect: Iterable[str] = ("ecli", "celex", "sector", "__source_window"),
) -> Tuple[pd.DataFrame, list]:
    """Drop every column that is entirely null / empty in *df*.

    The detection is data-driven rather than a hardcoded list: a column is
    only dropped if every cell is NaN or an empty/whitespace-only string.
    This keeps the pass idempotent — once dropped, it won't be dropped again
    on a re-run — and forward-compatible: if a future scrape populates a
    field, it stops being dropped automatically.

    Columns in *protect* are never dropped even if empty (keeps schema
    primitives like ``ecli`` / ``celex`` / ``sector`` stable).
    """
    protected = set(protect)
    dropped: list = []
    for col in df.columns:
        if col in protected:
            continue
        s = df[col]
        # Cells empty when null, empty string, or whitespace-only.
        nonempty = s.notna() & (s.astype("string").str.strip().str.len() > 0)
        if not nonempty.any():
            dropped.append(col)
    if dropped:
        df = df.drop(columns=dropped)
    return df, dropped


def dedup_by_ecli(
    df: pd.DataFrame,
    *,
    ecli_col: str = "ecli",
    source_window_col: str = "__source_window",
    extra_key_cols: Tuple[str, ...] = (),
) -> Tuple[pd.DataFrame, int]:
    """Collapse rows that share an ``ecli`` value to a single row.

    Returns ``(deduped_df, dropped_row_count)``. When ``__source_window`` is
    present and differs across the group, the surviving row keeps a
    ``;``-joined union of the source windows so provenance isn't lost.
    Rows with NaN ECLI are left alone — they can't be safely grouped.

    Pass ``extra_key_cols`` to widen the dedup key. For example, the
    post-multi-language ``fulltexts.parquet`` has one legitimate row per
    ``(ecli, text_language)`` — callers should pass
    ``extra_key_cols=("text_language",)`` so only true duplicates of the
    same language are collapsed.
    """
    if ecli_col not in df.columns:
        return df, 0
    before = len(df)

    valid = df[df[ecli_col].notna()].copy()
    nan_block = df[df[ecli_col].isna()].copy()

    # Full key is ecli plus any extras the caller asked for (defensive: only
    # use extras that actually exist in the frame).
    key_cols = [ecli_col] + [c for c in extra_key_cols if c in valid.columns]

    if source_window_col in valid.columns:
        # Build a per-key merged window string then deduplicate.
        merged = (
            valid.groupby(key_cols, sort=False)[source_window_col]
            .agg(lambda series: ";".join(sorted({str(s) for s in series if pd.notna(s) and str(s).strip()})))
        )
        valid = valid.drop_duplicates(subset=key_cols, keep="first")
        # Re-key the merged windows back onto each surviving row.
        merge_index = valid.set_index(key_cols).index
        valid[source_window_col] = merge_index.map(merged)
    else:
        valid = valid.drop_duplicates(subset=key_cols, keep="first")

    out = pd.concat([valid, nan_block], ignore_index=True)
    return out, before - len(out)


def write_viewer_friendly_parquet(
    df: pd.DataFrame,
    output: Path,
    row_group_size: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        output,
        row_group_size=row_group_size,
        compression="zstd",
        write_page_index=True,
    )


# ---------------------------------------------------------------------------
# Per-file pipelines
# ---------------------------------------------------------------------------


def clean_cases(
    df: pd.DataFrame,
    *,
    resolver,
) -> Tuple[pd.DataFrame, dict]:
    """Full cleanup for ``cases.parquet``.

    Order matters: dedup first (so the URI resolution only runs on unique
    rows), then resolve work_cites_work, then drop empty columns last (in
    case a column becomes empty *because* dedup removed its only non-empty
    row).
    """
    report: dict = {
        "rows_before": len(df),
        "rows_dedupped": 0,
        "columns_dropped": [],
        "uris_resolved": 0,
        "uris_unresolved": 0,
        "work_cites_rewritten_rows": 0,
    }

    df, dropped_count = dedup_by_ecli(df)
    report["rows_dedupped"] = dropped_count
    log.info("cases: deduped %d rows on ECLI -> %d rows", dropped_count, len(df))

    if "work_cites_work" in df.columns:
        uris = collect_unique_uris(df["work_cites_work"])
        log.info("cases: %d unique URIs in work_cites_work", len(uris))
        if uris:
            uri_to_celex = resolver(uris)
            log.info("cases: resolved %d / %d URIs", len(uri_to_celex), len(uris))
            new_col, stats = rewrite_column(df["work_cites_work"], uri_to_celex)
            df["work_cites_work"] = new_col
            report["uris_resolved"] = stats["uris_resolved"]
            report["uris_unresolved"] = stats["uris_unresolved"]
            report["work_cites_rewritten_rows"] = stats["rows_rewritten"]
        else:
            log.info("cases: work_cites_work already CELEX-form, skipping resolver")

    df, dropped_cols = drop_always_null_columns(df)
    report["columns_dropped"] = dropped_cols
    log.info("cases: dropped %d always-null columns: %s",
             len(dropped_cols), dropped_cols)

    report["rows_after"] = len(df)
    report["columns_after"] = df.shape[1]
    return df, report


def clean_fulltexts(df: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    """Cleanup for ``fulltexts.parquet`` — dedup on (ECLI, language).

    Post-multi-language fanout, fulltexts.parquet legitimately carries one
    row per (ECLI, text_language) pair. Dedup must therefore key on the
    pair, not just ECLI — otherwise we'd silently drop 22 of every 23
    language variants. ``text_language`` is left out of the key when the
    column isn't present (the pre-multi-lang shape), in which case dedup
    collapses on plain ECLI.
    """
    report = {"rows_before": len(df), "rows_dedupped": 0}
    df, dropped_count = dedup_by_ecli(
        df, extra_key_cols=("text_language",)
    )
    report["rows_dedupped"] = dropped_count
    report["rows_after"] = len(df)
    log.info("fulltexts: deduped %d rows on (ECLI, text_language) -> %d rows",
             dropped_count, len(df))
    return df, report


# ---------------------------------------------------------------------------
# Network-touching glue (injectable for tests)
# ---------------------------------------------------------------------------


def _resolve_uris(uris: Iterable[str]) -> dict:
    from cellar_extractor.sparql import resolve_celexes_for_cellar_uris  # type: ignore

    return resolve_celexes_for_cellar_uris(list(uris))


def _download(repo_id: str, filename: str, dest: Path) -> Path:
    from huggingface_hub import hf_hub_download  # type: ignore

    log.info("downloading %s from %s …", filename, repo_id)
    local = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        local_dir=str(dest.parent),
    )
    src = Path(local)
    if src.resolve() != dest.resolve():
        try:
            src.rename(dest)
        except OSError:
            dest.write_bytes(src.read_bytes())
    return dest


def _upload(repo_id: str, local_path: Path, repo_filename: str, token: str) -> None:
    from huggingface_hub import HfApi  # type: ignore

    api = HfApi(token=token)
    log.info("uploading %s -> %s:%s …", local_path.name, repo_id, repo_filename)
    api.upload_file(
        path_or_fileobj=str(local_path),
        path_in_repo=repo_filename,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=(
            f"Post-publish cleanup: viewer-friendly layout + "
            f"work_cites_work CELEX resolution + ECLI dedup ({repo_filename})"
        ),
    )


def run(
    repo_id: str,
    workdir: Path,
    *,
    dry_run: bool = False,
    token: str | None = None,
    resolver=_resolve_uris,
    downloader=_download,
    uploader=_upload,
) -> dict:
    """Drive the full two-file cleanup. Returns a report dict suitable for
    logging and for upstream callers to assert on."""
    workdir.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    if not dry_run and not token:
        raise SystemExit(
            "HUGGINGFACE_TOKEN required to upload. Set DRY_RUN=1 to skip the upload."
        )

    # --- cases.parquet ---
    cases_src = workdir / "cases.parquet"
    cases_dst = workdir / "cases.cleaned.parquet"
    downloader(repo_id, "cases.parquet", cases_src)
    cases_df = pd.read_parquet(cases_src)
    log.info("cases.parquet: %d rows × %d cols loaded", *cases_df.shape)
    cases_df, cases_report = clean_cases(cases_df, resolver=resolver)
    write_viewer_friendly_parquet(cases_df, cases_dst, CASES_ROW_GROUP_SIZE)
    log.info("cases.cleaned.parquet: %.1f MB", cases_dst.stat().st_size / 1e6)
    report["cases"] = cases_report

    # --- fulltexts.parquet ---
    fulltexts_src = workdir / "fulltexts.parquet"
    fulltexts_dst = workdir / "fulltexts.cleaned.parquet"
    downloader(repo_id, "fulltexts.parquet", fulltexts_src)
    fulltexts_df = pd.read_parquet(fulltexts_src)
    log.info("fulltexts.parquet: %d rows × %d cols loaded", *fulltexts_df.shape)
    fulltexts_df, fulltexts_report = clean_fulltexts(fulltexts_df)
    write_viewer_friendly_parquet(
        fulltexts_df, fulltexts_dst, FULLTEXTS_ROW_GROUP_SIZE
    )
    log.info("fulltexts.cleaned.parquet: %.1f MB", fulltexts_dst.stat().st_size / 1e6)
    report["fulltexts"] = fulltexts_report

    if dry_run:
        log.info("DRY_RUN — skipping upload. Files written to %s", workdir)
    else:
        uploader(repo_id, cases_dst, "cases.parquet", token)
        uploader(repo_id, fulltexts_dst, "fulltexts.parquet", token)
        log.info("upload complete. Viewer typically refreshes within ~60s.")

    return report


def main() -> int:
    repo_id = os.environ.get("HF_DATASET_REPO", "davidwickerhf/cjeu-opendata")
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")

    with tempfile.TemporaryDirectory(prefix="hf-cleanup-") as tmp:
        report = run(repo_id, Path(tmp), dry_run=dry_run, token=token)
    log.info("done. report: %s", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
