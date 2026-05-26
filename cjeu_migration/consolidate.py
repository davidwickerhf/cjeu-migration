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
            "decade_table":      [(decade, cases, with_text, coverage_pct), ...],
            "sector_table":      [(sector, cases, pct), ...],
            "fulltext_total":         int,
            "fulltext_with_text":     int,
            "fulltext_languages":     [(lang, count), ...],     # top 10
            "missing_reason_top":     [(reason, count), ...],   # top 5
            "dup_ecli_count":         int,
            "top_subjects":           [(atom, count), ...],     # top 15
            "top_procedures":         [(atom, count), ...],     # top 10
            "top_origin_countries":   [(country, count), ...],  # top 15
            "top_cited_cases":        [(ecli, count, subject, year), ...],  # top 10
            "citation_edges_total":   int,
            "citation_edges_internal": int,  # land inside the dataset
            "citation_edges_external": int,  # legislation, treaties, opinions
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
        "top_subjects": [],
        "top_procedures": [],
        "top_origin_countries": [],
        "top_cited_cases": [],
        "citation_edges_total": 0,
        "citation_edges_internal": 0,
        "citation_edges_external": 0,
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

    # ---- Top atom distributions (subject / procedure / country) ----
    def _atoms(col: str):
        if col not in cases_df.columns:
            return pd.Series(dtype="object")
        s = (
            cases_df[col]
            .astype("string")
            .fillna("")
            .str.split(";")
            .explode()
            .str.strip()
        )
        return s[s != ""]

    sm_atoms = _atoms("subject_matter")
    if not sm_atoms.empty:
        out["top_subjects"] = [
            (str(k), int(v)) for k, v in sm_atoms.value_counts().head(15).items()
        ]
    proc_atoms = _atoms("type_procedure")
    if not proc_atoms.empty:
        out["top_procedures"] = [
            (str(k), int(v)) for k, v in proc_atoms.value_counts().head(10).items()
        ]
    country_atoms = _atoms("origin_country")
    if not country_atoms.empty:
        out["top_origin_countries"] = [
            (str(k), int(v)) for k, v in country_atoms.value_counts().head(15).items()
        ]

    # ---- Citation graph topology ----
    if "work_cites_work" in cases_df.columns:
        edges = (
            cases_df["work_cites_work"]
            .astype("string")
            .fillna("")
            .str.split(";")
            .explode()
            .str.strip()
        )
        edges = edges[edges != ""]
        out["citation_edges_total"] = int(len(edges))
        if not edges.empty:
            # CELEX values in the dataset (split multi-cell entries too).
            case_celexes = set()
            for cell in cases_df.get("celex", pd.Series(dtype="object")).dropna():
                for c in str(cell).split(";"):
                    c = c.strip()
                    if c:
                        case_celexes.add(c)
            internal = int(edges.isin(case_celexes).sum())
            out["citation_edges_internal"] = internal
            out["citation_edges_external"] = int(len(edges) - internal)

    # ---- Top cited cases ----
    if "cited_by" in cases_df.columns:
        in_deg = (
            cases_df["cited_by"]
            .astype("string")
            .fillna("")
            .str.split(";")
            .map(lambda toks: sum(1 for t in toks if t.strip()))
        )
        # Pull the date-publication year for context, fall back to "".
        year_col = pd.Series([""] * len(cases_df), index=cases_df.index)
        if "date_publication" in cases_df.columns:
            def _first(v):
                if pd.isna(v):
                    return None
                parts = [p.strip() for p in str(v).split(";") if p.strip()]
                return min(parts) if parts else None
            dt = pd.to_datetime(
                cases_df["date_publication"].map(_first), errors="coerce", utc=True
            )
            year_col = dt.dt.year.astype("Int64").astype("string").fillna("")

        top_idx = in_deg.nlargest(10).index
        out["top_cited_cases"] = [
            (
                str(cases_df.loc[i, "ecli"]) if "ecli" in cases_df.columns else "",
                int(in_deg.loc[i]),
                str(cases_df.loc[i, "subject_matter"])
                    if "subject_matter" in cases_df.columns else "",
                str(year_col.loc[i]),
            )
            for i in top_idx
            if int(in_deg.loc[i]) > 0
        ]

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


def _format_top_atoms_table(rows: list, headers: tuple) -> str:
    """Format a (key, count) list as a 2-col markdown table."""
    if not rows:
        return "_(no data)_"
    lines = [
        f"| {headers[0]} | {headers[1]} |",
        "|---|---:|",
    ]
    for k, v in rows:
        lines.append(f"| {k} | {v:,} |")
    return "\n".join(lines)


def _format_top_cited_table(rows: list) -> str:
    """Format the most-cited-cases table."""
    if not rows:
        return "_(no citation data)_"
    lines = [
        "| Rank | ECLI | Cited by | Year | Subject matter |",
        "|---:|---|---:|:---:|---|",
    ]
    for i, (ecli, count, subject, year) in enumerate(rows, 1):
        # Truncate long subject_matter atoms for table readability
        subj = (subject[:60] + "…") if len(subject) > 60 else subject
        lines.append(f"| {i} | `{ecli}` | {count:,} | {year} | {subj} |")
    return "\n".join(lines)


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

    # --- Rich content blocks ---
    # Each block is only emitted when the relevant stats are available, so
    # the card degrades gracefully when called with minimal stats (e.g.
    # unit tests, empty corpora).
    if coverage_stats:
        ft_total = coverage_stats.get("fulltext_total", 0)
        ft_with = coverage_stats.get("fulltext_with_text", 0)
        ft_pct = (round(100 * ft_with / ft_total, 1) if ft_total else 0.0)

        # --- Coverage and caveats ---
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

- **ECLI dedup.** {coverage_stats.get('dup_ecli_count', 0)} duplicate-ECLI
  rows remain in this snapshot (down from 14 in earlier versions). The
  cleanup pipeline collapses same-ECLI rows from overlapping scrape
  windows; the surviving row carries a `;`-joined `__source_window`
  string for provenance.
- **Multi-window dedup at consolidation.** Raw scrape volume
  (~185 k ECLIs across all date windows) collapses to the unique ECLI
  set (~46 k) — CELLAR returns the same record from adjacent windows
  for backfilled pre-2000 entries.
- **Schema-trim.** 15 always-null CDM predicates (legislation-only
  fields like `eli`, `in_force`, plus text-describing fields that live
  in ``fulltexts.parquet``) are dropped from ``cases.parquet`` for
  schema clarity. The field reference in [`FIELDS.md`](FIELDS.md) is
  the source of truth.

"""

        # --- Citation graph topology block ---
        cg_total = coverage_stats.get("citation_edges_total", 0)
        cg_internal = coverage_stats.get("citation_edges_internal", 0)
        cg_external = coverage_stats.get("citation_edges_external", 0)
        cg_pct = (round(100 * cg_internal / cg_total, 1) if cg_total else 0.0)
        citation_graph_section = f"""## Citation graph

Each case row carries two citation columns:

- `work_cites_work` — outbound edges (CELEX IDs of cases / acts this case cites)
- `cited_by` — inbound edges (CELEX IDs of cases that cite this case)

Both are `;`-separated multi-cardinality strings. After the v2 cleanup
pass, both columns are CELEX-form (the previous URI form has been
resolved in place).

### Topology

- Total outbound edges: **{cg_total:,}**
- Inside the dataset (case → case, self-joinable): **{cg_internal:,} ({cg_pct}%)**
- Outside the dataset (case → legislation / treaty / opinion): **{cg_external:,}**

### Most-cited cases (top 10 by inbound count)

{_format_top_cited_table(coverage_stats.get("top_cited_cases", []))}

"""

        # --- Demographics block (subject / procedure / country) ---
        demographics_section = f"""## What's in the corpus

### Top subject-matter atoms

`subject_matter` is a `;`-separated list of EU legal-area atoms. The
single-atom counts below are after exploding the lists.

{_format_top_atoms_table(coverage_stats.get("top_subjects", []), ("Subject atom", "Cases"))}

### Top procedure types

{_format_top_atoms_table(coverage_stats.get("top_procedures", []), ("Procedure", "Cases"))}

### Top origin countries

`origin_country` records the member state that referred the case
(preliminary references) or whose national court the action originated
from. Counts after exploding multi-country cells.

{_format_top_atoms_table(coverage_stats.get("top_origin_countries", []), ("Country", "Cases"))}

"""
    else:
        coverage_section = ""
        citation_graph_section = ""
        demographics_section = ""

    # --- Static (data-independent) sections ---

    quick_start_section = f"""## Quick start

### Pandas (recommended for analytics)

```python
import pandas as pd

# Direct parquet — fastest, no datasets dep required.
URL = "https://huggingface.co/datasets/{hf_dataset_repo}/resolve/main"
cases = pd.read_parquet(f"{{URL}}/cases.parquet")
texts = pd.read_parquet(f"{{URL}}/fulltexts.parquet")

print(cases.shape, texts.shape)
```

### HuggingFace `datasets` (streaming-friendly)

```python
from datasets import load_dataset

cases = load_dataset("{hf_dataset_repo}", "cases", split="train")
texts = load_dataset("{hf_dataset_repo}", "fulltexts", split="train")

# Streaming for the fulltexts table (avoids loading 325 MB into RAM):
texts_stream = load_dataset(
    "{hf_dataset_repo}", "fulltexts", split="train", streaming=True
)
for row in texts_stream:
    print(row["ecli"], len(row["text"]))
    break
```

### Polars (fast column scans)

```python
import polars as pl

cases = pl.read_parquet(
    "https://huggingface.co/datasets/{hf_dataset_repo}/resolve/main/cases.parquet"
)
print(cases.select(["ecli", "celex", "subject_matter"]).head())
```

"""

    recipes_section = """## Recipes

Practical queries you can paste verbatim. All examples assume `cases`
and `texts` DataFrames loaded as in *Quick start*.

### 1. Filter by date range and sector

```python
import pandas as pd

cases["pub_date"] = pd.to_datetime(
    cases["date_publication"].str.split(";").str[0],
    errors="coerce", utc=True,
)
recent_cjeu = cases[
    (cases["pub_date"] >= "2020-01-01")
    & (cases["sector"] == "6")
]
print(len(recent_cjeu), "post-2020 sector-6 cases")
```

### 2. Subject-matter filter (preliminary references on VAT)

```python
vat_refs = cases[
    cases["subject_matter"].fillna("").str.contains("Value added tax")
    & cases["type_procedure"].fillna("").str.contains(
        "Reference for a preliminary ruling"
    )
]
print(f"{len(vat_refs):,} VAT preliminary references")
print(vat_refs[["ecli", "origin_country", "date_publication"]].head())
```

### 3. Build a citation network (in-dataset edges only)

```python
import pandas as pd

# work_cites_work is CELEX-form; explode for one edge per row.
edges = (
    cases[["celex", "work_cites_work"]]
    .assign(target=lambda d: d["work_cites_work"].str.split(";"))
    .explode("target")
    .dropna(subset=["target"])
    .query("target != ''")
    .rename(columns={"celex": "source"})
    [["source", "target"]]
)
# Restrict to edges whose target is in the dataset (self-joinable):
in_dataset = set(cases["celex"].dropna())
edges = edges[edges["target"].isin(in_dataset)]
print(f"{len(edges):,} case→case citation edges")
```

### 4. PageRank over the citation graph (with NetworkX)

```python
import networkx as nx

g = nx.from_pandas_edgelist(
    edges, source="source", target="target", create_using=nx.DiGraph,
)
pr = nx.pagerank(g, alpha=0.85)
top = sorted(pr.items(), key=lambda kv: -kv[1])[:10]
for celex, score in top:
    row = cases.loc[cases["celex"] == celex].iloc[0]
    print(f"{row['ecli']:30s}  PR={score:.4f}  {row['subject_matter'][:60]}")
```

### 5. Fetch the body text for a specific case

```python
# texts has one row per (celex, language). Modern cases publish in ~24
# languages; you usually want the procedural language (matches `language_procedure`).
def fetch_text(ecli: str, lang: str | None = None) -> str | None:
    rows = texts[texts["ecli"] == ecli]
    if rows.empty:
        return None
    if lang:
        rows = rows[rows["text_language"].str.upper() == lang.upper()]
    if rows.empty:
        return None
    return rows.iloc[0]["text"]

body = fetch_text("ECLI:EU:C:2014:317")  # the Google Spain case
print(body[:500] if body else "(no fulltext available)")
```

### 6. Per-language fulltext counts

```python
print(
    texts[texts["text"].str.len() >= 200]
    ["text_language"].value_counts().head(15)
)
```

### 7. Cases without fulltext, with the reason recorded

```python
no_text = texts[texts["text"].str.len() < 200]
print(no_text["missing_reasons"].value_counts().head())
```

### 8. Join cases and texts on ECLI

```python
joined = cases.merge(texts, on="ecli", how="left", suffixes=("", "_text"))
print(joined.shape)  # ~46 k rows (one per ECLI; no fan-out post-dedup)
```

"""

    fulltext_analysis_section = """## Working with fulltexts

`fulltexts.parquet` has 8 columns:

| Column | Type | Description |
|---|---|---|
| `ecli` | string | Join key against `cases.parquet`. |
| `celex` | string | Sometimes multi-valued (`62019CJ0793;62019CJ0793_RES`) when an ECLI bundles multiple work items. |
| `text` | string | Plain text. Empty when CELLAR has no body for this work. |
| `text_source` | string | `CELLAR_ITEM` / `CELLAR_REST_XHTML` / `INFOCURIA_BLOB_HTML` / `EXTRACTOR_FALLBACK_TEXT`. |
| `text_format` | string | `html` / `xhtml` / `pdf` / `xml`. Original markup format before plain-text extraction. |
| `text_language` | string | ISO 639-1 code (`FR`, `EN`, `DE`, …). The procedural language at the CJEU is historically French, hence FR dominance. |
| `missing_reasons` | string | `;`-separated tags explaining empty fields, e.g. `FULLTEXT_UNAVAILABLE_UPSTREAM`. |
| `__source_window` | string | Internal: the date window(s) that scraped this row. `;`-joined post-dedup. |

The body is plain text — no markup, no headers/footers, no page numbers.
For semantic analysis (sentence segmentation, embeddings) just iterate
over `text` directly:

```python
from itertools import islice

def iter_long_judgments(min_chars: int = 5000):
    long_ones = texts[texts["text"].str.len() >= min_chars]
    for _, row in long_ones.iterrows():
        yield row["ecli"], row["text_language"], row["text"]

for ecli, lang, body in islice(iter_long_judgments(), 3):
    print(f"--- {ecli} ({lang}, {len(body):,} chars) ---")
    print(body[:300], "…\\n")
```

"""

    extraction_section = """## How the data was extracted

The pipeline lives in two repos:

1. **[`cellar-extractor`](https://github.com/maastrichtlawtech/cellar-extractor)** — the actual scraper. Hits the CELLAR SPARQL endpoint
   for metadata + citation graph, plus InfoCuria + CELLAR REST for body text and provenance flags. Handles per-CDM-predicate flattening, sector-3 legislation support, citation URI→CELEX resolution.

2. **[`cjeu-migration`](https://github.com/davidwickerhf/cjeu-migration)** — the orchestrator. Iterates date windows (month-sized), retries failed
   windows with exponential backoff, persists per-window CSV + JSON checkpoints (survives crashes), then consolidates everything into the two
   parquet files you're reading here. Includes the cleanup scripts (`scripts/cleanup_hf_dataset.py`) that produced this version.

Single-command reproduction:

```bash
git clone https://github.com/davidwickerhf/cjeu-migration
cd cjeu-migration
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # set HUGGINGFACE_TOKEN if you intend to upload
cjeu-migrate run --skip-upload  # local-only; drop --skip-upload to push to HF
```

The full corpus run (1954 → today) takes ~4-6 h on a 16-thread CPU instance.

### Field provenance

See [`FIELDS.md`](FIELDS.md) for per-field source documentation: which CDM
predicate or InfoCuria key produced each column, type, cardinality, and
whether it's case-law-only or any-sector.

"""

    schema_section = f"""## Schema

### Canonical columns

These are the contracted columns — they appear on every row (populated or
null), and their semantics are stable across runs:

{canonical_md}

### Discovered columns

CDM predicates surfaced opportunistically from CELLAR that aren't part of
the canonical contract but were populated for at least one row in this
snapshot:

{discovered_md}

For a full field reference (type, cardinality, source, examples), see
[`FIELDS.md`](FIELDS.md) bundled with this dataset.

"""

    citation_section = """## How to cite

If you use this dataset in academic work, please cite both the source
software and the underlying CELLAR / InfoCuria publications:

```bibtex
@misc{cjeu-opendata-2026,
  title  = {CJEU / CELLAR Case Law},
  author = {Wicker, David},
  year   = {2026},
  publisher = {HuggingFace},
  howpublished = {\\url{https://huggingface.co/datasets/davidwickerhf/cjeu-opendata}},
}

@software{cellar-extractor,
  title  = {cellar-extractor: a Python toolkit for CJEU corpus extraction},
  author = {{Maastricht Law \\& Tech}},
  url    = {https://github.com/maastrichtlawtech/cellar-extractor},
}
```

"""

    license_section = """## License

- **Code (extraction + consolidation pipeline):** Apache-2.0.
- **Dataset content:** EU institutional content. Court judgments are
  public-domain in the EU; metadata and citation graph are derived from
  the EU's CELLAR open-data programme.

Re-use is unconstrained for research, teaching, and commercial purposes;
attribution to the source (CELLAR / curia.europa.eu) is appreciated.
"""

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

A full-coverage corpus of European Court of Justice case law — every
judgment, order, opinion, and notice the Court of Justice and General
Court have published since **1954**, plus the **national-court decisions**
that cite EU law (sector 8). Each case carries its own metadata,
multi-language full text where available, and the inbound + outbound
**citation graph** as joinable CELEX identifiers.

The dataset is a single source of truth for empirical EU-law research:
who cites whom, what subject matters dominate, how the procedural-language
mix has shifted over seven decades, where the General Court's IP docket
fits in.

| Table | Granularity | Description |
|---|---|---|
| `cases.parquet` | one row per ECLI | All case metadata. 107 columns covering court formation, judicial procedure type, advocate general, judge rapporteur, subject matter, eurovoc concepts, citing / cited-by edges, etc. Per-field docs in [`FIELDS.md`](FIELDS.md). |
| `fulltexts.parquet` | one row per ECLI | Plain-text body of each document plus provenance flags (`text_source`, `text_format`, `text_language`, `missing_reasons`). |

## Headline numbers

- **Date range:** {start_date} → {end_date}
- **Cases:** {cases_rows:,}
- **Fulltexts:** {fulltexts_rows:,}
- **Last refreshed:** {now}

{coverage_section}{quick_start_section}{recipes_section}{citation_graph_section}{demographics_section}{fulltext_analysis_section}{extraction_section}{schema_section}{citation_section}{license_section}"""
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
