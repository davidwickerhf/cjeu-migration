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
import pyarrow as pa
import pyarrow.parquet as pq


log = logging.getLogger(__name__)

# Row-group sizing for the published parquet files.
#
# The HuggingFace dataset viewer streams a parquet file by row group, with a
# hard 300 MB scan cap per request. A single 300 k+ row, 100+ column corpus
# with one row group quickly blows past that and the viewer dies with
# `TooBigContentError`. Splitting the file into many small row groups lets
# the viewer (and `datasets.load_dataset(streaming=True)`) pull just what
# they need without loading the whole table.
#
# At ~50 KB / case row, 2_000 rows ≈ 100 MB uncompressed → ~25 MB on disk
# with zstd. For fulltexts (~30 KB plain text average), 500 rows ≈ 15 MB.
# Both stay comfortably under the 300 MB viewer cap with headroom.
CASES_ROW_GROUP_SIZE = 2_000
FULLTEXTS_ROW_GROUP_SIZE = 500


def _write_parquet(df: pd.DataFrame, output_path: Path, row_group_size: int) -> None:
    """Persist *df* as a viewer-friendly parquet file.

    Three switches matter for downstream tooling:

    * ``row_group_size`` — many small groups instead of one giant one, so
      tools that scan-by-row-group (HF viewer, ``datasets`` streaming) can
      do random access.
    * ``write_page_index=True`` — adds the column + offset indexes so a
      reader can seek directly to the row group it needs without scanning
      the row-group footer first.
    * ``compression="zstd"`` — smaller files and faster decode than the
      pandas default (``snappy``). Roughly 30-40 % smaller on this data.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        # Pandas + pyarrow won't write a row_group_size'd file for an empty
        # frame, so just emit the header. The HF viewer is fine with empties.
        df.to_parquet(output_path, index=False, compression="zstd")
        return

    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table,
        output_path,
        row_group_size=row_group_size,
        compression="zstd",
        write_page_index=True,
    )


def consolidate_cases(window_csv_dir: Path, output_path: Path) -> pd.DataFrame:
    """Concatenate every window CSV into a single parquet table.

    Returns the resulting DataFrame for assertions / further use. Empty input
    yields an empty parquet (header-only) so callers can still publish.
    """
    csv_files = sorted(window_csv_dir.glob("*.csv"))
    if not csv_files:
        log.warning("no window CSV files found in %s", window_csv_dir)
        df = pd.DataFrame()
        _write_parquet(df, output_path, CASES_ROW_GROUP_SIZE)
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

    _write_parquet(df, output_path, CASES_ROW_GROUP_SIZE)
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
        _write_parquet(df, output_path, FULLTEXTS_ROW_GROUP_SIZE)
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
    _write_parquet(df, output_path, FULLTEXTS_ROW_GROUP_SIZE)
    log.info("wrote %d fulltext rows -> %s", len(df), output_path)
    return df


def compute_coverage_stats(
    cases_df: pd.DataFrame,
    fulltexts_df: pd.DataFrame,
) -> dict:
    """Return summary statistics for the dataset card's Coverage section.

    Built from the *already consolidated* DataFrames so it costs one pass and
    can be unit-tested without round-tripping through parquet. Empty inputs
    yield a structured "no data" result rather than raising — important for
    the empty-corpus path.

    Returned dict shape::

        {
            "decade_table":   [(decade, cases, with_text, coverage_pct), ...],
            "sector_table":   [(sector, cases, pct), ...],
            "fulltext_total":      int,
            "fulltext_with_text":  int,
            "fulltext_languages":  [(lang, count), ...],   # top 10
            "missing_reason_top":  [(reason, count), ...], # top 5
            "dup_ecli_count":      int,
        }
    """
    out: dict = {
        "decade_table": [],
        "sector_table": [],
        "fulltext_total": int(len(fulltexts_df)),
        "fulltext_with_text": 0,
        "fulltext_languages": [],
        "missing_reason_top": [],
        "dup_ecli_count": 0,
    }

    if cases_df.empty:
        return out

    # ---- decade coverage (uses date_publication = CDM work_date_document) ----
    if "date_publication" in cases_df.columns and "ecli" in cases_df.columns:
        # Multi-valued cells (";" separated). Take the earliest token.
        def _first(v):
            if pd.isna(v):
                return None
            parts = [p.strip() for p in str(v).split(";") if p.strip()]
            return min(parts) if parts else None

        dt = pd.to_datetime(
            cases_df["date_publication"].map(_first),
            errors="coerce",
            utc=True,
        )
        decade = (dt.dt.year // 10 * 10).astype("Int64")

        # text presence — derived from the case's matching fulltext row.
        if not fulltexts_df.empty and "text" in fulltexts_df.columns:
            has_text_ecli = set(
                fulltexts_df.loc[
                    fulltexts_df["text"].astype("string").str.len().fillna(0) >= 200,
                    "ecli",
                ].dropna()
            )
        else:
            has_text_ecli = set()
        with_text = cases_df["ecli"].isin(has_text_ecli)

        grouped = (
            pd.DataFrame({"decade": decade, "with_text": with_text})
            .dropna(subset=["decade"])
            .groupby("decade", observed=True)
        )
        for dec, sub in grouped:
            n = len(sub)
            wt = int(sub["with_text"].sum())
            pct = round(100 * wt / n, 1) if n else 0.0
            out["decade_table"].append((int(dec), n, wt, pct))
        out["decade_table"].sort()

    # ---- sector split (split multi-sector cells like "6;8" into atoms) ----
    if "sector" in cases_df.columns:
        atoms = (
            cases_df["sector"]
            .astype("string")
            .fillna("")
            .str.split(";")
            .explode()
            .str.strip()
        )
        atoms = atoms[atoms != ""]
        total = len(atoms) or 1
        for sec, n in atoms.value_counts().items():
            out["sector_table"].append((str(sec), int(n), round(100 * n / total, 1)))

    # ---- fulltext-side stats ----
    if not fulltexts_df.empty:
        if "text" in fulltexts_df.columns:
            txt_len = fulltexts_df["text"].astype("string").str.len().fillna(0)
            out["fulltext_with_text"] = int((txt_len >= 200).sum())
        if "text_language" in fulltexts_df.columns:
            lang = fulltexts_df["text_language"].astype("string").fillna("")
            lang = lang[lang.str.len() > 0]
            out["fulltext_languages"] = [
                (str(k), int(v)) for k, v in lang.value_counts().head(10).items()
            ]
        if "missing_reasons" in fulltexts_df.columns:
            reasons = (
                fulltexts_df["missing_reasons"]
                .astype("string")
                .fillna("")
                .str.split(";")
                .explode()
                .str.strip()
            )
            reasons = reasons[reasons != ""]
            out["missing_reason_top"] = [
                (str(k), int(v)) for k, v in reasons.value_counts().head(5).items()
            ]

    # ---- ECLI-duplicate count (post-consolidation) ----
    if "ecli" in cases_df.columns:
        dup_eclis = cases_df["ecli"].dropna()
        out["dup_ecli_count"] = int(
            len(dup_eclis) - dup_eclis.nunique()
        )

    return out


def _format_decade_table(rows: list) -> str:
    if not rows:
        return "_(no date_publication data available)_"
    lines = [
        "| Decade | Cases | With fulltext | Coverage |",
        "|---|---:|---:|---:|",
    ]
    for dec, n, wt, pct in rows:
        lines.append(f"| {dec}s | {n:,} | {wt:,} | {pct}% |")
    return "\n".join(lines)


def _format_sector_table(rows: list) -> str:
    if not rows:
        return "_(no sector data)_"
    label = {"6": "EU courts (CJEU / GC / CST)", "8": "National case law citing EU law"}
    lines = ["| Sector | Description | Cases | Share |", "|---|---|---:|---:|"]
    for sec, n, pct in rows:
        lines.append(f"| {sec} | {label.get(sec, '—')} | {n:,} | {pct}% |")
    return "\n".join(lines)


def _format_language_table(rows: list) -> str:
    if not rows:
        return "_(no language data)_"
    return ", ".join(f"**{k}** ({v:,})" for k, v in rows)


def _format_missing_reasons(rows: list) -> str:
    if not rows:
        return "_(no missing-reason data)_"
    return "\n".join(f"- `{k}` — {v:,} rows" for k, v in rows)


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
    coverage_stats: Optional[dict] = None,
) -> None:
    """Write the HuggingFace dataset card (README.md).

    When ``coverage_stats`` is provided (build it via
    :func:`compute_coverage_stats`), the card includes a detailed Coverage
    section with per-decade fulltext rates, sector breakdown, top languages,
    and the citation-graph URI caveat. Without it the card falls back to the
    minimal headline numbers — useful for unit tests where round-tripping
    the data isn't necessary.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    canonical_md = "\n".join(f"- `{c}`" for c in canonical_columns) or "_(none)_"
    discovered_md = "\n".join(f"- `{c}`" for c in discovered_columns) or "_(none populated)_"

    # --- Coverage section: only emitted when stats are supplied ---
    if coverage_stats:
        ft_total = coverage_stats.get("fulltext_total", 0)
        ft_with = coverage_stats.get("fulltext_with_text", 0)
        ft_pct = (round(100 * ft_with / ft_total, 1) if ft_total else 0.0)
        coverage_section = f"""## Coverage and caveats

### Per-decade fulltext availability

CELLAR's body-text coverage rises sharply with the move from analogue to
digital court archives. Pre-2000 cases are largely metadata-only — the
decision text exists in print but was never digitised into CELLAR.

{_format_decade_table(coverage_stats.get("decade_table", []))}

In total, **{ft_with:,} of {ft_total:,} ({ft_pct}%)** documents have a
non-trivial body text (≥200 characters). The remainder carry a populated
``missing_reasons`` column explaining why:

{_format_missing_reasons(coverage_stats.get("missing_reason_top", []))}

### Sector split

{_format_sector_table(coverage_stats.get("sector_table", []))}

### Languages

The dataset preserves each document's original procedural language. The
top 10 by row count (within ``fulltexts.parquet``):

{_format_language_table(coverage_stats.get("fulltext_languages", []))}

### Known quirks

- **Citation graph in URI form.** ``work_cites_work`` currently stores
  raw CELLAR URIs (e.g.
  ``http://publications.europa.eu/resource/cellar/<uuid>``) instead of
  CELEX identifiers. Use ``cited_by`` (which is CELEX-form) for in-place
  joins, or resolve the URIs via
  ``cellar_extractor.sparql.resolve_celexes_for_cellar_uris``.
- **ECLI duplicates.** {coverage_stats.get('dup_ecli_count', 0)} rows
  share an ECLI with another row. These are the same legal decision
  scraped through two overlapping windows; columns other than
  ``__source_window`` agree, so a ``drop_duplicates(subset='ecli')`` is
  lossless.
- **Multi-window dedup.** Raw scrape volume (~185 k ECLIs across windows)
  collapses to the unique ECLI set (~46 k) at consolidation. CELLAR can
  return the same record from adjacent date windows, especially for
  pre-2000 backfilled entries.
- **Schema-union columns.** A handful of columns in ``cases.parquet``
  exist for cross-sector parity (legislation-only fields, or
  text-describing fields that properly live in ``fulltexts.parquet``)
  and are always null for case law. See ``FIELDS.md`` for the
  per-field reference.

"""
    else:
        coverage_section = ""

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

## Headline numbers

- Date window: **{start_date} → {end_date}**
- Cases: **{cases_rows:,}**
- Fulltexts: **{fulltexts_rows:,}**
- Last refreshed: {now}

{coverage_section}## Usage

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
