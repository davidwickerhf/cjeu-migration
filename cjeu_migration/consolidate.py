"""Combine per-window CSV + fulltext JSON files into HF-ready parquet.

Output layout in ``consolidated_dir``::

    cases.parquet         one row per ECLI, all canonical + discovered columns
    fulltexts.parquet     one row per (celex, language) with the plain-text body
    README.md             HF dataset card with a schema summary
    FIELDS.md             copied from cellar-extractor for the per-field catalogue

The cases parquet column set is the *union* of columns observed across all
windows: schema drift between windows (some CDM predicates only appearing on
later years) is handled by filling missing values with null.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd


log = logging.getLogger(__name__)


def consolidate_cases(window_csv_dir: Path, output_path: Path) -> pd.DataFrame:
    """Concatenate every window CSV into a single parquet table.

    Returns the resulting DataFrame for assertions / further use. Empty input
    yields an empty parquet (header-only) so callers can still publish.
    """
    csv_files = sorted(window_csv_dir.glob("*.csv"))
    if not csv_files:
        log.warning("no window CSV files found in %s", window_csv_dir)
        df = pd.DataFrame()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, index=False)
        return df

    frames: List[pd.DataFrame] = []
    for path in csv_files:
        try:
            frame = pd.read_csv(path, dtype=str)
        except pd.errors.EmptyDataError:
            log.warning("empty CSV in window %s — skipping", path.name)
            continue
        if frame.empty:
            continue
        frame["__source_window"] = path.stem
        frames.append(frame)

    if not frames:
        df = pd.DataFrame()
    else:
        df = pd.concat(frames, ignore_index=True, sort=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    log.info("wrote %d cases rows -> %s", len(df), output_path)
    return df


def consolidate_fulltexts(window_json_dir: Path, output_path: Path) -> pd.DataFrame:
    """Concatenate every window fulltext JSON into a single parquet table.

    Each input file is a list of ``{celex, ecli, text, text_source, ...}``
    dicts (the shape cellar-extractor writes). The output is one row per
    document, with ``__source_window`` added so users can join back to a window.
    """
    json_files = sorted(window_json_dir.glob("*.json"))
    if not json_files:
        log.warning("no fulltext JSON files found in %s", window_json_dir)
        df = pd.DataFrame()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, index=False)
        return df

    rows: List[dict] = []
    for path in json_files:
        try:
            with path.open("r", encoding="utf-8") as f:
                entries = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("unreadable fulltext JSON %s: %s — skipping", path.name, exc)
            continue
        if not isinstance(entries, list):
            log.warning(
                "fulltext JSON %s wasn't a list (got %s) — skipping",
                path.name, type(entries).__name__,
            )
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            entry["__source_window"] = path.stem
            rows.append(entry)

    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_path, index=False)
    log.info("wrote %d fulltext rows -> %s", len(df), output_path)
    return df


def write_dataset_card(
    output_path: Path,
    *,
    cases_rows: int,
    fulltexts_rows: int,
    start_date: str,
    end_date: str,
    canonical_columns: List[str],
    discovered_columns: List[str],
    hf_dataset_repo: str,
) -> None:
    """Write the HuggingFace dataset card (README.md)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    canonical_md = "\n".join(f"- `{c}`" for c in canonical_columns) or "_(none)_"
    discovered_md = "\n".join(f"- `{c}`" for c in discovered_columns) or "_(none populated)_"
    content = f"""---
license: apache-2.0
language:
  - en
size_categories:
  - 10K<n<100K
task_categories:
  - text-classification
  - text-retrieval
pretty_name: CJEU / CELLAR Case Law
configs:
  - config_name: cases
    data_files: cases.parquet
  - config_name: fulltexts
    data_files: fulltexts.parquet
---

# CJEU / CELLAR Case Law

European Court of Justice case law (CELLAR sectors 6 and 8) scraped via
[`cellar-extractor`](https://github.com/maastrichtlawtech/cellar-extractor)
and consolidated into two Parquet tables:

| Table | Granularity | Description |
|---|---|---|
| `cases.parquet` | one row per ECLI | All canonical CJEU metadata fields (court formation, judicial procedure type, language, origin country, advocate general, judge rapporteur, subject matter, eurovoc concepts, citing / cited_by graph, etc.). Per-field documentation in [`FIELDS.md`](FIELDS.md). |
| `fulltexts.parquet` | one row per `(celex, language)` | Plain-text body of each document, with provenance flags (`text_source`, `text_format`, `text_language`, `missing_reasons`). |

## Coverage

- Date window: **{start_date} → {end_date}**
- Cases: **{cases_rows:,}**
- Fulltexts: **{fulltexts_rows:,}**
- Last refreshed: {now}

## Usage

```python
from datasets import load_dataset

cases = load_dataset("{hf_dataset_repo}", "cases", split="train")
texts = load_dataset("{hf_dataset_repo}", "fulltexts", split="train")

# Join — note the relationship is one ECLI : many CELEX-language pairs.
import pandas as pd
df = cases.to_pandas().merge(texts.to_pandas(), on="ecli", how="left")
```

## Canonical schema

These columns are present on every row (populated or null), as documented in
[`FIELDS.md`](FIELDS.md):

{canonical_md}

## Discovered fields (also present in this snapshot)

CDM predicates surfaced from CELLAR that aren't part of the canonical schema
but were populated for at least one row in this run:

{discovered_md}

## Source

Extracted via [`cellar-extractor`](https://github.com/maastrichtlawtech/cellar-extractor)
against the live CELLAR SPARQL endpoint and InfoCuria. See
[`FIELDS.md`](FIELDS.md) for per-field upstream provenance.

## License

Apache-2.0 (matching the source code license). Underlying judicial documents
are public domain / EU institutional content.
"""
    output_path.write_text(content, encoding="utf-8")


FIELDS_MD_RAW_URL = (
    "https://raw.githubusercontent.com/"
    "maastrichtlawtech/cellar-extractor/dev/FIELDS.md"
)


def copy_fields_md(output_path: Path) -> bool:
    """Make ``FIELDS.md`` available in the dataset directory.

    Tries the installed cellar-extractor distribution first (works when the
    file is included in the package's MANIFEST). Falls back to fetching the
    raw file from GitHub at install-time URL, since the upstream wheel does
    not currently include ``FIELDS.md`` in its MANIFEST.in.

    Returns ``True`` if the file ended up on disk, ``False`` otherwise (the
    dataset card still links the canonical column list, so a missing
    ``FIELDS.md`` is recoverable).
    """
    # 1. Local install candidates (will work once upstream MANIFEST.in is fixed).
    for candidate in _locate_fields_md():
        try:
            output_path.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            log.info("copied FIELDS.md from %s -> %s", candidate, output_path)
            return True
        except OSError as exc:
            log.warning("could not read %s: %s", candidate, exc)

    # 2. Fall back to fetching the raw markdown from GitHub.
    try:
        import urllib.request

        with urllib.request.urlopen(FIELDS_MD_RAW_URL, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        output_path.write_text(content, encoding="utf-8")
        log.info("downloaded FIELDS.md from %s -> %s", FIELDS_MD_RAW_URL, output_path)
        return True
    except Exception as exc:  # network down, repo moved, etc.
        log.warning(
            "FIELDS.md not in install and could not be fetched from %s: %s — "
            "skipping copy. The dataset card still lists canonical columns.",
            FIELDS_MD_RAW_URL, exc,
        )
        return False


def _locate_fields_md() -> List[Path]:
    """Find the ``FIELDS.md`` that may ship with cellar-extractor.

    The file isn't a Python module so we walk a few likely install locations.
    Returns an empty list when the file isn't present (upstream MANIFEST.in
    doesn't currently include it — see ``copy_fields_md`` fallback).
    """
    found: List[Path] = []
    try:
        import cellar_extractor  # type: ignore
    except ImportError:
        return found

    package_dir = Path(cellar_extractor.__file__).resolve().parent
    # 1. Repo root next to the package (editable installs).
    candidate = package_dir.parent / "FIELDS.md"
    if candidate.exists():
        found.append(candidate)
    # 2. Inside the package itself (would require MANIFEST.in change upstream).
    candidate = package_dir / "FIELDS.md"
    if candidate.exists():
        found.append(candidate)
    return found
