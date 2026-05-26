"""Regenerate and upload the README.md for a published HF dataset.

Downloads the live parquet files, recomputes the coverage stats, renders a
fresh README via ``cjeu_migration.consolidate.write_dataset_card``, and
uploads it back. The parquet files themselves are not touched — this is
strictly a metadata refresh, useful when you've improved the card template
without re-running the extraction.

Usage::

    # Dry run — render the README locally, no upload.
    DRY_RUN=1 python scripts/regenerate_hf_readme.py

    # Upload to HF (HUGGINGFACE_TOKEN must be set).
    python scripts/regenerate_hf_readme.py

    # Override the target repo.
    HF_DATASET_REPO=org/name python scripts/regenerate_hf_readme.py

    # Use already-downloaded parquets instead of fetching from HF.
    LOCAL_CASES_PARQUET=/path/cases.parquet \\
    LOCAL_FULLTEXTS_PARQUET=/path/fulltexts.parquet \\
    python scripts/regenerate_hf_readme.py

The dataset-window dates come from min/max ``date_publication`` in the
cases parquet, so the rendered README always reflects the live span.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

from cjeu_migration.consolidate import (
    compute_coverage_stats,
    write_dataset_card,
)


log = logging.getLogger("regenerate_hf_readme")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without network)
# ---------------------------------------------------------------------------


def _first(v):
    if pd.isna(v):
        return None
    parts = [p.strip() for p in str(v).split(";") if p.strip()]
    return min(parts) if parts else None


def derive_date_window(cases_df: pd.DataFrame) -> tuple[str, str]:
    """Pull the actual min/max ``date_publication`` from the corpus, in
    ``YYYY-MM-DD`` form. Falls back to ``""`` when the column is missing
    or unparseable, so the card emits empty dates rather than raising."""
    if "date_publication" not in cases_df.columns or cases_df.empty:
        return "", ""
    dt = pd.to_datetime(cases_df["date_publication"].map(_first),
                        errors="coerce", utc=True)
    if dt.notna().sum() == 0:
        return "", ""
    return (dt.min().date().isoformat(), dt.max().date().isoformat())


def derive_column_lists(cases_df: pd.DataFrame) -> tuple[list, list]:
    """Split the live cases columns into (canonical, discovered) — using
    ``cellar_extractor.schema`` if available, falling back to empty
    canonical / everything-is-discovered when the extractor isn't installed
    (CI for the script alone).
    """
    try:
        from cellar_extractor import schema  # type: ignore
        canonical = list(schema.CANONICAL_COLUMNS)
    except Exception:
        canonical = []
    canonical_in_data = [c for c in canonical if c in cases_df.columns]
    discovered = sorted(
        c for c in cases_df.columns
        if c not in canonical and not c.startswith("__")
    )
    return canonical_in_data, discovered


# ---------------------------------------------------------------------------
# Pipeline glue
# ---------------------------------------------------------------------------


def _download(repo_id: str, filename: str, dest: Path) -> Path:
    from huggingface_hub import hf_hub_download  # type: ignore
    log.info("downloading %s from %s …", filename, repo_id)
    local = hf_hub_download(
        repo_id=repo_id, filename=filename, repo_type="dataset",
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
        commit_message="Refresh dataset card with usage examples + recipes",
    )


def run(
    repo_id: str,
    workdir: Path,
    *,
    dry_run: bool = False,
    token: str | None = None,
    local_cases: Path | None = None,
    local_fulltexts: Path | None = None,
    downloader=_download,
    uploader=_upload,
) -> Path:
    """Top-level pipeline. Returns the path of the rendered README."""
    workdir.mkdir(parents=True, exist_ok=True)

    if not dry_run and not token:
        raise SystemExit(
            "HUGGINGFACE_TOKEN required to upload. Set DRY_RUN=1 to skip."
        )

    # Read parquets (download if local copies not supplied)
    if local_cases:
        cases_path = Path(local_cases)
    else:
        cases_path = workdir / "cases.parquet"
        downloader(repo_id, "cases.parquet", cases_path)
    if local_fulltexts:
        fulltexts_path = Path(local_fulltexts)
    else:
        fulltexts_path = workdir / "fulltexts.parquet"
        downloader(repo_id, "fulltexts.parquet", fulltexts_path)

    log.info("loading cases  ← %s", cases_path)
    cases_df = pd.read_parquet(cases_path)
    log.info("loading fulltexts ← %s", fulltexts_path)
    fulltexts_df = pd.read_parquet(fulltexts_path)
    log.info("cases: %d × %d   fulltexts: %d × %d",
             *cases_df.shape, *fulltexts_df.shape)

    # Compute stats + window
    stats = compute_coverage_stats(cases_df, fulltexts_df)
    start_date, end_date = derive_date_window(cases_df)
    canonical, discovered = derive_column_lists(cases_df)
    log.info("date window: %s → %s", start_date, end_date)
    log.info("canonical: %d cols, discovered: %d cols",
             len(canonical), len(discovered))
    log.info("citation graph: %d edges total, %d internal",
             stats.get("citation_edges_total", 0),
             stats.get("citation_edges_internal", 0))

    readme_path = workdir / "README.md"
    write_dataset_card(
        readme_path,
        cases_rows=len(cases_df),
        fulltexts_rows=len(fulltexts_df),
        start_date=start_date,
        end_date=end_date,
        canonical_columns=canonical,
        discovered_columns=discovered,
        hf_dataset_repo=repo_id,
        coverage_stats=stats,
    )
    log.info("rendered README → %s (%d bytes)", readme_path, readme_path.stat().st_size)

    if dry_run:
        log.info("DRY_RUN — skipping upload.")
    else:
        uploader(repo_id, readme_path, "README.md", token)
        log.info("upload complete. HF page refreshes within ~30s.")
    return readme_path


def main() -> int:
    repo_id = os.environ.get("HF_DATASET_REPO", "davidwickerhf/cjeu-opendata")
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    local_cases = os.environ.get("LOCAL_CASES_PARQUET")
    local_fulltexts = os.environ.get("LOCAL_FULLTEXTS_PARQUET")

    with tempfile.TemporaryDirectory(prefix="hf-readme-") as tmp:
        readme = run(
            repo_id, Path(tmp), dry_run=dry_run, token=token,
            local_cases=Path(local_cases) if local_cases else None,
            local_fulltexts=Path(local_fulltexts) if local_fulltexts else None,
        )
        if dry_run:
            log.info("Output README left at %s", readme)
            # Copy to the repo's working dir so the user can inspect it.
            try:
                import shutil
                shutil.copy(readme, Path.cwd() / "README.preview.md")
                log.info("Preview copied to %s", Path.cwd() / "README.preview.md")
            except Exception as exc:
                log.warning("could not copy preview: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
