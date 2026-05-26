"""Resolve work_cites_work URIs to CELEX on an already-published HF dataset.

Use this script when the dataset was produced by a cellar-extractor version
that left raw CELLAR URIs in ``work_cites_work``. It downloads the cases
parquet, replaces every URI in ``work_cites_work`` with its CELEX form
(reusing the same SPARQL resolver the extractor uses), re-serializes with
viewer-friendly settings, and uploads the new file back to the dataset.

Idempotent: re-running on an already-fixed file is a no-op (every cell is
either CELEX-form already or empty).

Usage::

    # Dry run — fetch, resolve, write side-by-side, NO upload.
    DRY_RUN=1 python scripts/fix_hf_work_cites_work.py

    # Actually push to HF (requires HUGGINGFACE_TOKEN).
    HUGGINGFACE_TOKEN=hf_xxx python scripts/fix_hf_work_cites_work.py

    # Override the target repo (defaults to davidwickerhf/cjeu-opendata).
    HF_DATASET_REPO=org/name python scripts/fix_hf_work_cites_work.py

Requires ``pip install huggingface_hub pandas pyarrow cellar-extractor``.
The script picks up cellar-extractor's resolver via the installed package.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


log = logging.getLogger("fix_hf_work_cites_work")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


# Match cjeu_migration.consolidate so the rewritten file keeps the same
# viewer-friendly layout (small row groups + page index + zstd).
CASES_ROW_GROUP_SIZE = 2_000


# ---------------------------------------------------------------------------
# Core transform — pure-Python so it's unit-testable without HF / SPARQL.
# ---------------------------------------------------------------------------


def collect_unique_uris(series: pd.Series) -> list:
    """Return the sorted set of HTTP URIs appearing in any cell of *series*.

    Cells are ``;``-separated lists; empty / NaN cells are skipped. The
    return value is the input to the batched SPARQL resolver.
    """
    out: set = set()
    for raw in series.dropna():
        for token in str(raw).split(";"):
            token = token.strip()
            if token.startswith("http"):
                out.add(token)
    return sorted(out)


def rewrite_column(
    series: pd.Series,
    uri_to_celex: dict,
) -> Tuple[pd.Series, dict]:
    """Map every URI in *series* to its CELEX using *uri_to_celex*.

    Returns ``(rewritten_series, stats)`` where ``stats`` is a small report
    so the caller can log progress. Empty cells, all-CELEX cells, and cells
    that mix URIs with already-resolved CELEXes are all preserved verbatim
    where no rewrite is possible — the script is meant to be safe to re-run.
    """
    stats = {
        "rows_total": len(series),
        "rows_with_uri": 0,
        "rows_rewritten": 0,
        "uris_seen": 0,
        "uris_resolved": 0,
        "uris_unresolved": 0,
    }
    rewritten = []
    for raw in series:
        # Normalise empty / NaN / whitespace-only cells to "" so the output
        # column has consistent emptiness — matches the convention the rest
        # of the dataset already uses for missing multi-cardinality fields.
        if pd.isna(raw) or not str(raw).strip():
            rewritten.append("")
            continue
        tokens = [t.strip() for t in str(raw).split(";") if t.strip()]
        any_uri = any(t.startswith("http") for t in tokens)
        if not any_uri:
            rewritten.append(";".join(tokens))
            continue
        stats["rows_with_uri"] += 1
        out_tokens: list = []
        seen: set = set()
        for t in tokens:
            if t.startswith("http"):
                stats["uris_seen"] += 1
                mapped = uri_to_celex.get(t)
                if mapped:
                    stats["uris_resolved"] += 1
                    if mapped not in seen:
                        seen.add(mapped)
                        out_tokens.append(mapped)
                else:
                    stats["uris_unresolved"] += 1
                    # Drop the unresolved URI rather than letting it stay in
                    # the output — the cellar-extractor fix does the same.
            else:
                if t not in seen:
                    seen.add(t)
                    out_tokens.append(t)
        new_value = ";".join(out_tokens)
        if new_value != str(raw):
            stats["rows_rewritten"] += 1
        rewritten.append(new_value)
    return pd.Series(rewritten, index=series.index, dtype="object"), stats


def write_viewer_friendly_parquet(df: pd.DataFrame, output: Path) -> None:
    """Same layout cjeu_migration.consolidate uses for new dataset uploads."""
    output.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        output,
        row_group_size=CASES_ROW_GROUP_SIZE,
        compression="zstd",
        write_page_index=True,
    )


# ---------------------------------------------------------------------------
# Pipeline glue (network-touching)
# ---------------------------------------------------------------------------


def _resolve_uris(uris: Iterable[str]) -> dict:
    """Wrap cellar_extractor's SPARQL resolver so the script can be imported
    and tested without the package installed (a stub gets monkey-patched in
    tests)."""
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
        # Force a fresh fetch — no symlink shenanigans:
        local_dir_use_symlinks=False,
    )
    # local_dir_use_symlinks is the older API; on newer hub versions we get
    # a real file back regardless. Normalise to dest.
    src = Path(local)
    if src.resolve() != dest.resolve():
        try:
            src.rename(dest)
        except OSError:
            # Fallback: read+write (different filesystem etc.)
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
            "Resolve work_cites_work URIs to CELEX (in-place rewrite)"
        ),
    )


def run(
    repo_id: str,
    workdir: Path,
    dry_run: bool = False,
    token: str | None = None,
    *,
    resolver=_resolve_uris,
    downloader=_download,
    uploader=_upload,
) -> dict:
    """Top-level pipeline.

    Injection hooks (``resolver`` / ``downloader`` / ``uploader``) exist so
    the test suite can drive this end-to-end without any network access.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "cases.parquet"
    dst = workdir / "cases.fixed.parquet"

    downloader(repo_id, "cases.parquet", src)
    df = pd.read_parquet(src)
    log.info("loaded %d rows × %d cols from cases.parquet", len(df), df.shape[1])

    if "work_cites_work" not in df.columns:
        raise SystemExit("cases.parquet has no work_cites_work column — bailing.")

    uris = collect_unique_uris(df["work_cites_work"])
    log.info("unique URIs in work_cites_work: %d", len(uris))

    if not uris:
        log.info("no URIs to resolve — column is already CELEX-form. Nothing to do.")
        return {"rows_rewritten": 0, "uris_resolved": 0, "uris_unresolved": 0}

    uri_to_celex = resolver(uris)
    log.info("resolved %d / %d URIs to CELEX", len(uri_to_celex), len(uris))

    new_col, stats = rewrite_column(df["work_cites_work"], uri_to_celex)
    df["work_cites_work"] = new_col
    log.info(
        "rewrite stats: rows with URI=%d, rewritten=%d, uris_resolved=%d, unresolved=%d",
        stats["rows_with_uri"], stats["rows_rewritten"],
        stats["uris_resolved"], stats["uris_unresolved"],
    )

    write_viewer_friendly_parquet(df, dst)
    log.info("wrote rewritten parquet: %s (%.1f MB)",
             dst, dst.stat().st_size / 1e6)

    if dry_run:
        log.info("DRY_RUN — skipping upload. Inspect %s", dst)
    else:
        if not token:
            raise SystemExit(
                "HUGGINGFACE_TOKEN required to upload. "
                "Set DRY_RUN=1 to skip the upload."
            )
        uploader(repo_id, dst, "cases.parquet", token)
        log.info("upload complete. Viewer typically refreshes within ~60s.")

    return stats


def main() -> int:
    repo_id = os.environ.get("HF_DATASET_REPO", "davidwickerhf/cjeu-opendata")
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")

    with tempfile.TemporaryDirectory(prefix="hf-fix-") as tmp:
        run(repo_id, Path(tmp), dry_run=dry_run, token=token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
