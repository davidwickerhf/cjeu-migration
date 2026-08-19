"""Retroactively supplement an already-published HF dataset with the
multi-language CELLAR manifestations the extractor missed.

Background. The v2 extraction was started before cellar-extractor PR #7
(unconditional InfoCuria → CELLAR supplementation for sector 6) merged into
``dev``. Pre-2001 ECLIs got the multi-language fanout via the existing
fallback path; post-2001 ECLIs went through InfoCuria-only and ended up
with ~1.3 languages/case instead of ~23.

This script reads the published dataset, finds every ECLI whose language
coverage is below a configurable threshold, queries CELLAR for the
missing manifestations, and appends new rows to ``fulltexts.parquet``.
Existing rows are never overwritten — only languages not currently in the
dataset are added. Idempotent: re-running on an already-topped-up dataset
is a no-op.

Usage::

    # Dry run — patch the parquet locally, no upload.
    DRY_RUN=1 python scripts/topup_multilang_fulltexts.py

    # Upload to HF (HUGGINGFACE_TOKEN must be set).
    python scripts/topup_multilang_fulltexts.py

    # Override target repo / threshold / parallelism.
    HF_DATASET_REPO=org/name \
    MIN_LANGS=24 YEAR_THRESHOLD=0 WORKERS=6 \
    python scripts/topup_multilang_fulltexts.py

Progress is uploaded to HF every ``CHECKPOINT_EVERY`` processed ECLIs
(default 2000; 0 disables checkpointing). A killed box therefore loses at
most one checkpoint interval — the July 2026 run uploaded only at the very
end and lost everything when its box was destroyed.

Default thresholds are deliberately aggressive: ``MIN_LANGS=24`` (the
current number of official EU languages — anything below that gets
re-checked) and ``YEAR_THRESHOLD=0`` (every decade). Cases that are
already at CELLAR's available maximum (e.g. a 1965 judgment that
genuinely caps at 6 languages) probe-and-skip cheaply; the only real
cost is one SPARQL round-trip per ECLI. The point is to make the
defaults exhaustive — if a case has fewer translations than what
CELLAR exposes, we want to find that out.

Requires the locally-installed cellar-extractor to be the post-PR-#7
version. Reinstall with::

    pip install --force-reinstall --no-deps \\
      "cellar-extractor @ git+https://github.com/maastrichtlawtech/cellar-extractor.git@dev"
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


log = logging.getLogger("topup_multilang_fulltexts")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


FULLTEXTS_ROW_GROUP_SIZE = 500


# ---------------------------------------------------------------------------
# Pure transforms — unit-testable without network or HF
# ---------------------------------------------------------------------------


def find_sparse_eclis(
    cases_df: pd.DataFrame,
    fulltexts_df: pd.DataFrame,
    *,
    min_langs: int,
    year_threshold: int,
) -> list:
    """Return the list of ``(ecli, celex)`` tuples whose language coverage
    is below ``min_langs`` AND whose publication year is ``>=
    year_threshold``.

    The year threshold mirrors the InfoCuria-only era in v2: pre-2001 cases
    already went through the CELLAR fallback path and have full multi-lang
    coverage, so we skip them. The threshold is configurable so the script
    can also be used to top-up *all* sparse cases by setting it to 0.

    Multi-cell ``celex`` values are split — the first token is used as the
    SPARQL lookup key.
    """
    if cases_df.empty or fulltexts_df.empty:
        return []

    # Count distinct languages per ECLI in fulltexts.parquet
    lang_counts = (
        fulltexts_df.assign(
            _lang=fulltexts_df["text_language"].astype("string").str.upper().fillna("")
        )
        .query("_lang != ''")
        .groupby("ecli")["_lang"]
        .nunique()
    )

    # Parse year from date_publication (multi-cell aware — take earliest)
    def _first_date(v):
        if pd.isna(v):
            return None
        parts = [p.strip() for p in str(v).split(";") if p.strip()]
        return min(parts) if parts else None

    dt = pd.to_datetime(
        cases_df["date_publication"].map(_first_date), errors="coerce", utc=True
    )
    years = dt.dt.year

    # First non-null CELEX token
    def _first_celex(v):
        # Prefer a token without INF and strip _SUM/_INF-style suffixes —
        # the raw first token can be the summary document's CELEX (e.g.
        # "62020CJ0414_SUM;62020CJ0414"), whose work family carries only a
        # partial language set. 3,642 corpus cases have suffixed tokens.
        if pd.isna(v):
            return None
        parts = [p.strip() for p in str(v).split(";") if p.strip()]
        if not parts:
            return None
        non_inf = [p for p in parts if "INF" not in p]
        return (non_inf[0] if non_inf else parts[0]).split("_")[0]

    celexes = cases_df["celex"].map(_first_celex)

    sparse: list = []
    for idx, (ecli, year, celex) in enumerate(zip(cases_df["ecli"], years, celexes)):
        if pd.isna(ecli) or not celex:
            continue
        if pd.isna(year) or year < year_threshold:
            continue
        current_langs = int(lang_counts.get(ecli, 0))
        if current_langs < min_langs:
            sparse.append((str(ecli), str(celex)))
    return sparse


def merge_new_rows(
    fulltexts_df: pd.DataFrame,
    new_rows: list,
) -> Tuple[pd.DataFrame, int]:
    """Append *new_rows* to *fulltexts_df*, skipping any (ecli, language)
    pairs that already exist. Returns ``(updated_df, added_count)``."""
    if not new_rows:
        return fulltexts_df, 0
    new_df = pd.DataFrame(new_rows)

    if fulltexts_df.empty:
        return new_df, len(new_df)

    # Build the set of (ecli, language) pairs already present.
    existing_keys = set(zip(
        fulltexts_df["ecli"].astype("string").fillna(""),
        fulltexts_df["text_language"].astype("string").str.upper().fillna(""),
    ))

    keep_mask = []
    for _, row in new_df.iterrows():
        key = (str(row.get("ecli") or ""),
               str(row.get("text_language") or "").upper())
        keep_mask.append(key not in existing_keys)

    new_df = new_df.loc[keep_mask].reset_index(drop=True)
    if new_df.empty:
        return fulltexts_df, 0

    merged = pd.concat([fulltexts_df, new_df], ignore_index=True, sort=False)
    return merged, len(new_df)


def write_viewer_friendly_parquet(df: pd.DataFrame, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(
        table, output,
        row_group_size=FULLTEXTS_ROW_GROUP_SIZE,
        compression="zstd",
        write_page_index=True,
    )


# ---------------------------------------------------------------------------
# Streaming helpers — used by run() to avoid loading the 5+ GB text column
# into pandas. The text column is what blows memory in container
# environments (a 5 GB on-disk parquet expands to 15-25 GB once pandas
# decodes every string into a Python object). These helpers stream the
# parquet via pyarrow without ever materialising the text column.
# ---------------------------------------------------------------------------


def stream_existing_langs_by_ecli(
    fulltexts_path: Path,
    *,
    batch_size: int = 10_000,
) -> dict:
    """Build a per-ECLI language index by streaming fulltexts.parquet.

    Returns ``dict[ecli] -> set[upper-cased language code]``. The text
    column is never read — pyarrow projects only ``(ecli, text_language)``.
    Memory use is bounded by ``batch_size`` plus the index dict itself
    (~one set per ECLI, typically a few MB total even for 50k ECLIs).
    """
    pf = pq.ParquetFile(fulltexts_path)
    out: dict = {}
    for batch in pf.iter_batches(
        batch_size=batch_size, columns=["ecli", "text_language"]
    ):
        ecli_col = batch.column("ecli").to_pylist()
        lang_col = batch.column("text_language").to_pylist()
        for e, l in zip(ecli_col, lang_col):
            if not e:
                continue
            lang = (l or "").upper()
            if not lang:
                continue
            out.setdefault(e, set()).add(lang)
    return out


def find_sparse_eclis_from_index(
    cases_df: pd.DataFrame,
    existing_by_ecli: dict,
    *,
    min_langs: int,
    year_threshold: int,
) -> list:
    """Variant of :func:`find_sparse_eclis` that takes the precomputed
    ``existing_by_ecli`` index instead of a full fulltexts DataFrame.

    Output is identical to ``find_sparse_eclis`` given equivalent inputs.
    Used by :func:`run` so the script doesn't need to materialise the
    text column in pandas.
    """
    if cases_df.empty:
        return []

    def _first_date(v):
        if pd.isna(v):
            return None
        parts = [p.strip() for p in str(v).split(";") if p.strip()]
        return min(parts) if parts else None

    dt = pd.to_datetime(
        cases_df["date_publication"].map(_first_date), errors="coerce", utc=True
    )
    years = dt.dt.year

    def _first_celex(v):
        # Prefer a token without INF and strip _SUM/_INF-style suffixes —
        # the raw first token can be the summary document's CELEX (e.g.
        # "62020CJ0414_SUM;62020CJ0414"), whose work family carries only a
        # partial language set. 3,642 corpus cases have suffixed tokens.
        if pd.isna(v):
            return None
        parts = [p.strip() for p in str(v).split(";") if p.strip()]
        if not parts:
            return None
        non_inf = [p for p in parts if "INF" not in p]
        return (non_inf[0] if non_inf else parts[0]).split("_")[0]

    celexes = cases_df["celex"].map(_first_celex)

    sparse: list = []
    for ecli, year, celex in zip(cases_df["ecli"], years, celexes):
        if pd.isna(ecli) or not celex:
            continue
        if pd.isna(year) or year < year_threshold:
            continue
        if len(existing_by_ecli.get(str(ecli), ())) < min_langs:
            sparse.append((str(ecli), str(celex)))
    return sparse


def _union_schema(orig: pa.Schema, extra: pa.Schema) -> pa.Schema:
    """Return a schema with all fields from *orig* plus any new fields
    from *extra*. Original field types win on collision."""
    fields = list(orig)
    existing = {f.name for f in fields}
    for f in extra:
        if f.name not in existing:
            fields.append(f)
    return pa.schema(fields)


def _conform_batch(batch: pa.RecordBatch, target: pa.Schema) -> pa.RecordBatch:
    """Reproject *batch* to match *target* schema, filling missing
    columns with typed nulls."""
    arrays = []
    for field in target:
        if field.name in batch.schema.names:
            arrays.append(batch.column(field.name))
        else:
            arrays.append(pa.nulls(batch.num_rows, type=field.type))
    return pa.RecordBatch.from_arrays(arrays, schema=target)


def append_new_rows_streaming(
    original_path: Path,
    new_rows: list,
    output_path: Path,
) -> int:
    """Stream-copy *original_path* to *output_path* and append *new_rows*
    that don't already exist as (ecli, text_language) pairs.

    Memory-bounded — reads via pyarrow batches, writes via
    :class:`ParquetWriter` without ever materialising the whole table.
    Schema is the union of the original parquet's schema and any new
    fields present in *new_rows* (with nulls padded for original rows).
    Output is viewer-friendly (zstd + page index).

    Returns the count of new rows actually appended (after dedup).
    """
    if not new_rows:
        return 0

    reader = pq.ParquetFile(original_path)
    orig_schema = reader.schema_arrow

    # Pass 1 — collect (ecli, language) keys already present.
    existing_keys: set = set()
    for batch in reader.iter_batches(
        batch_size=20_000, columns=["ecli", "text_language"]
    ):
        for e, l in zip(
            batch.column("ecli").to_pylist(),
            batch.column("text_language").to_pylist(),
        ):
            if e and l:
                existing_keys.add((str(e), str(l).upper()))

    deduped = [
        r for r in new_rows
        if (
            str(r.get("ecli") or ""),
            (r.get("text_language") or "").upper(),
        ) not in existing_keys
    ]
    if not deduped:
        return 0

    new_table = pa.Table.from_pandas(pd.DataFrame(deduped), preserve_index=False)
    target_schema = _union_schema(orig_schema, new_table.schema)

    # Pass 2 — stream original through, then append new rows.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(
        output_path,
        schema=target_schema,
        compression="zstd",
        write_page_index=True,
    )
    try:
        # New reader because iter_batches in pass 1 exhausted the iterator state
        reader2 = pq.ParquetFile(original_path)
        for batch in reader2.iter_batches(batch_size=FULLTEXTS_ROW_GROUP_SIZE):
            writer.write_batch(_conform_batch(batch, target_schema))
        for new_batch in new_table.to_batches(max_chunksize=FULLTEXTS_ROW_GROUP_SIZE):
            writer.write_batch(_conform_batch(new_batch, target_schema))
    finally:
        writer.close()

    return len(deduped)


def stream_stub_index(
    fulltexts_path: Path,
    *,
    ratio: float = 0.25,
    min_median: int = 10_000,
    batch_size: int = 2_000,
) -> dict:
    """Find stub texts: rows whose length is far below the case's median.

    These are headnotes / OJ notices captured (mostly from InfoCuria)
    before the full CELLAR manifestation existed. The language *counts* as
    covered, so the MIN_LANGS topup never revisits it — this index feeds
    the upgrade mode, which does.

    Returns ``dict[ecli] -> dict[LANG] -> current_length`` for every row
    with ``length < ratio * median(case lengths)`` where the median itself
    is ``>= min_median`` (short cases where every rendition is small are
    not stubs).
    """
    per_ecli: dict = {}
    pf = pq.ParquetFile(fulltexts_path)
    for batch in pf.iter_batches(
        batch_size=batch_size, columns=["ecli", "text_language", "text"]
    ):
        eclis = batch.column("ecli").to_pylist()
        langs = batch.column("text_language").to_pylist()
        # lengths only — the text strings are dropped with the batch
        lens = [len(t) if t else 0 for t in batch.column("text").to_pylist()]
        for e, l, n in zip(eclis, langs, lens):
            lang = (l or "").upper()
            if not e or not lang or n <= 0:
                continue
            per_ecli.setdefault(e, []).append((lang, n))

    stubs: dict = {}
    for ecli, pairs in per_ecli.items():
        lengths = sorted(n for _, n in pairs)
        mid = len(lengths) // 2
        median = (lengths[mid] if len(lengths) % 2
                  else (lengths[mid - 1] + lengths[mid]) / 2)
        if median < min_median:
            continue
        for lang, n in pairs:
            if n < ratio * median:
                stubs.setdefault(ecli, {})[lang] = n
    return stubs


def replace_rows_streaming(
    original_path: Path,
    replacement_rows: list,
    output_path: Path,
) -> int:
    """Stream-copy *original_path* to *output_path*, DROPPING rows whose
    (ecli, language) matches a replacement, then append the replacements.

    The upgrade counterpart of :func:`append_new_rows_streaming` — that one
    refuses to touch existing keys; this one exists precisely to supersede
    them. Returns the number of replacement rows written.
    """
    if not replacement_rows:
        return 0

    # last-in wins per key, so a retried ECLI can't duplicate
    by_key = {}
    for r in replacement_rows:
        key = (str(r.get("ecli") or ""), (r.get("text_language") or "").upper())
        by_key[key] = r
    replace_keys = set(by_key)

    reader = pq.ParquetFile(original_path)
    orig_schema = reader.schema_arrow
    new_table = pa.Table.from_pandas(
        pd.DataFrame(list(by_key.values())), preserve_index=False)
    target_schema = _union_schema(orig_schema, new_table.schema)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(
        output_path, schema=target_schema,
        compression="zstd", write_page_index=True,
    )
    try:
        for batch in reader.iter_batches(batch_size=FULLTEXTS_ROW_GROUP_SIZE):
            eclis = batch.column("ecli").to_pylist()
            langs = batch.column("text_language").to_pylist()
            mask = [
                (str(e or ""), (l or "").upper()) not in replace_keys
                for e, l in zip(eclis, langs)
            ]
            if not all(mask):
                batch = batch.filter(pa.array(mask))
            if batch.num_rows:
                writer.write_batch(_conform_batch(batch, target_schema))
        for nb in new_table.to_batches(max_chunksize=FULLTEXTS_ROW_GROUP_SIZE):
            writer.write_batch(_conform_batch(nb, target_schema))
    finally:
        writer.close()
    return len(by_key)


# ---------------------------------------------------------------------------
# CELLAR query (per ECLI) — uses cellar-extractor's helpers
# ---------------------------------------------------------------------------


def _topup_one_ecli(
    ecli: str,
    celex: str,
    existing_langs: set,
    *,
    work_uri_fn,
    items_fn,
    fanout_fn,
) -> list:
    """For one ECLI, fetch CELLAR manifestations and return new fulltext
    rows (ones in languages not in *existing_langs*).

    The four function arguments are the cellar-extractor SPARQL helpers,
    injected so the unit tests can stub them out.
    """
    try:
        work_uri = work_uri_fn(celex, sector="6")
    except Exception as exc:
        log.warning("work_uri lookup failed for %s: %s", celex, exc)
        return []
    if not work_uri:
        return []

    try:
        candidates = items_fn(work_uri)
    except Exception as exc:
        log.warning("items fetch failed for %s: %s", celex, exc)
        return []

    try:
        cellar_fulltexts = fanout_fn(candidates, source_label="CELLAR_ITEM")
    except Exception as exc:
        log.warning("fanout failed for %s: %s", celex, exc)
        return []

    new_rows = []
    for ft in cellar_fulltexts:
        lang = (ft.get("text_language") or "").upper()
        if not lang or lang in existing_langs:
            continue
        new_rows.append({
            "celex": celex,
            "ecli": ecli,
            "text": ft.get("text", ""),
            "text_source": "CELLAR_ITEM",
            "text_language": lang,
            "text_format": ft.get("text_format", ""),
            "missing_reasons": "",
            "__source_window": "topup_v2_multilang",
        })
    return new_rows


def _upgrade_one_ecli(
    ecli: str,
    celex: str,
    stub_langs: dict,
    *,
    work_uri_fn,
    items_fn,
    fanout_fn,
) -> list:
    """For one ECLI, fetch CELLAR manifestations for its stub languages and
    return replacement rows — only where the fetched text is materially
    longer (> 2x) than the stored stub, so a genuine short rendition is
    never clobbered by an equally short refetch.
    """
    try:
        work_uri = work_uri_fn(celex, sector="6")
    except Exception as exc:
        log.warning("work_uri lookup failed for %s: %s", celex, exc)
        return []
    if not work_uri:
        return []

    try:
        candidates = items_fn(work_uri)
    except Exception as exc:
        log.warning("items fetch failed for %s: %s", celex, exc)
        return []
    wanted = [c for c in candidates
              if (c.get("language") or "").upper() in stub_langs]
    if not wanted:
        return []

    try:
        cellar_fulltexts = fanout_fn(wanted, source_label="CELLAR_ITEM")
    except Exception as exc:
        log.warning("fanout failed for %s: %s", celex, exc)
        return []

    rows = []
    for ft in cellar_fulltexts:
        lang = (ft.get("text_language") or "").upper()
        text = ft.get("text") or ""
        if lang not in stub_langs or len(text) <= 2 * stub_langs[lang]:
            continue
        rows.append({
            "celex": celex,
            "ecli": ecli,
            "text": text,
            "text_source": "CELLAR_ITEM",
            "text_language": lang,
            "text_format": ft.get("text_format", ""),
            "missing_reasons": "",
            "__source_window": "upgrade_stub_texts",
        })
    return rows


def topup_dataset(
    cases_df: pd.DataFrame,
    fulltexts_df: pd.DataFrame,
    *,
    min_langs: int = 24,
    year_threshold: int = 0,
    max_workers: int = 6,
    work_uri_fn=None,
    items_fn=None,
    fanout_fn=None,
) -> Tuple[pd.DataFrame, dict]:
    """Orchestrator. Returns ``(updated_fulltexts_df, stats_dict)``.

    The three ``*_fn`` parameters default to cellar-extractor's helpers but
    can be stubbed for tests.
    """
    if work_uri_fn is None or items_fn is None or fanout_fn is None:
        from cellar_extractor.eurlex_scraping import (
            _fetch_sector8_items_for_celex as _default_items_for_celex,
            _fanout_fulltexts_from_candidates as _default_fanout,
        )
        # The production path unions manifestations across every CELLAR
        # work sharing the CELEX (a single arbitrary work missed languages
        # — nine sampled judgments lost their English text that way). The
        # two-step seam is kept for the tests: the default work_uri_fn
        # passes the celex through as the token items_fn consumes.
        if work_uri_fn is None and items_fn is None:
            work_uri_fn = lambda celex, sector="6": celex
            items_fn = lambda celex: _default_items_for_celex(celex, sector="6")[1]
        fanout_fn = fanout_fn or _default_fanout

    sparse = find_sparse_eclis(
        cases_df, fulltexts_df,
        min_langs=min_langs, year_threshold=year_threshold,
    )
    log.info("found %d sparse ECLIs to topup (year>=%d, langs<%d)",
             len(sparse), year_threshold, min_langs)

    # Precompute per-ECLI existing language sets so we don't recompute them
    # in the per-task call.
    existing_by_ecli: dict = {}
    if not fulltexts_df.empty:
        grouped = (
            fulltexts_df.assign(
                _lang=fulltexts_df["text_language"].astype("string").str.upper().fillna("")
            )
            .groupby("ecli")["_lang"]
            .apply(lambda s: set(v for v in s if v))
        )
        existing_by_ecli = grouped.to_dict()

    all_new_rows: list = []
    failures = 0
    start = time.monotonic()
    processed = 0

    def _task(ecli_celex):
        ecli, celex = ecli_celex
        existing = existing_by_ecli.get(ecli, set())
        return _topup_one_ecli(
            ecli, celex, existing,
            work_uri_fn=work_uri_fn,
            items_fn=items_fn,
            fanout_fn=fanout_fn,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, sc): sc for sc in sparse}
        for fut in as_completed(futures):
            processed += 1
            try:
                rows = fut.result()
                all_new_rows.extend(rows)
            except Exception as exc:
                failures += 1
                log.warning("topup failed for %s: %s", futures[fut], exc)
            if processed % 100 == 0:
                rate = processed / (time.monotonic() - start)
                remaining = len(sparse) - processed
                eta_min = (remaining / rate / 60) if rate else 0
                log.info("topup progress: %d/%d  (+%d rows so far, "
                         "%.1f ECLIs/s, ETA %.1f min)",
                         processed, len(sparse), len(all_new_rows),
                         rate, eta_min)

    updated_df, added = merge_new_rows(fulltexts_df, all_new_rows)
    log.info("topup complete: %d new rows appended (after dedup), %d failures",
             added, failures)

    return updated_df, {
        "sparse_eclis": len(sparse),
        "new_rows_attempted": len(all_new_rows),
        "new_rows_added": added,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Network glue (injectable)
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
        repo_id=repo_id, repo_type="dataset",
        commit_message=(
            "Top-up multi-language fulltexts for post-2001 ECLIs "
            "(retro-supplement matching cellar-extractor PR #7)"
        ),
    )


def run(
    repo_id: str,
    workdir: Path,
    *,
    dry_run: bool = False,
    token: str | None = None,
    min_langs: int = 24,
    year_threshold: int = 0,
    max_workers: int = 6,
    checkpoint_every: int = 0,
    local_cases: Path | None = None,
    local_fulltexts: Path | None = None,
    downloader=_download,
    uploader=_upload,
    work_uri_fn=None,
    items_fn=None,
    fanout_fn=None,
) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)

    if not dry_run and not token:
        raise SystemExit(
            "HUGGINGFACE_TOKEN required to upload. Set DRY_RUN=1 to skip."
        )

    cases_path = Path(local_cases) if local_cases else workdir / "cases.parquet"
    fulltexts_path = (
        Path(local_fulltexts) if local_fulltexts else workdir / "fulltexts.parquet"
    )
    if not local_cases:
        downloader(repo_id, "cases.parquet", cases_path)
    if not local_fulltexts:
        downloader(repo_id, "fulltexts.parquet", fulltexts_path)

    log.info("loading cases  ← %s", cases_path)
    cases_df = pd.read_parquet(cases_path)

    # Stream-build a per-ECLI language index without ever loading the 5+ GB
    # text column into pandas. The text column is what blows memory on
    # container-limited boxes — a 5 GB on-disk parquet inflates to 20+ GB
    # of Python string objects in pandas. Streaming keeps us bounded.
    log.info("indexing fulltexts (streaming) ← %s", fulltexts_path)
    existing_by_ecli = stream_existing_langs_by_ecli(fulltexts_path)
    log.info(
        "indexed %d ECLIs across %d (ecli, lang) tuples",
        len(existing_by_ecli),
        sum(len(s) for s in existing_by_ecli.values()),
    )

    if work_uri_fn is None or items_fn is None or fanout_fn is None:
        from cellar_extractor.eurlex_scraping import (
            _fetch_sector8_items_for_celex as _default_items_for_celex,
            _fanout_fulltexts_from_candidates as _default_fanout,
        )
        # The production path unions manifestations across every CELLAR
        # work sharing the CELEX (a single arbitrary work missed languages
        # — nine sampled judgments lost their English text that way). The
        # two-step seam is kept for the tests: the default work_uri_fn
        # passes the celex through as the token items_fn consumes.
        if work_uri_fn is None and items_fn is None:
            work_uri_fn = lambda celex, sector="6": celex
            items_fn = lambda celex: _default_items_for_celex(celex, sector="6")[1]
        fanout_fn = fanout_fn or _default_fanout

    sparse = find_sparse_eclis_from_index(
        cases_df, existing_by_ecli,
        min_langs=min_langs, year_threshold=year_threshold,
    )
    log.info(
        "found %d sparse ECLIs to topup (year>=%d, langs<%d)",
        len(sparse), year_threshold, min_langs,
    )

    pending_rows: list = []
    total_generated = 0
    total_added = 0
    failures = 0
    start = time.monotonic()
    processed = 0
    ckpt_n = 0
    current_base = fulltexts_path

    def _flush(rows: list) -> int:
        # Append *rows* onto the current base parquet and (unless DRY_RUN)
        # upload the result to HF. Each flush becomes the base for the next
        # one, so a killed box loses at most one checkpoint interval of work
        # — the run that died before ever uploading is what motivated this.
        nonlocal ckpt_n, current_base
        if not rows:
            return 0
        ckpt_n += 1
        out = workdir / f"fulltexts.ckpt{ckpt_n % 2}.parquet"
        added = append_new_rows_streaming(current_base, rows, out)
        if added == 0:
            log.info("checkpoint %d: all %d rows were duplicates", ckpt_n, len(rows))
            return 0
        current_base = out
        if dry_run:
            log.info("checkpoint %d: +%d rows (DRY_RUN — not uploaded)", ckpt_n, added)
        else:
            uploader(repo_id, out, "fulltexts.parquet", token)
            log.info("checkpoint %d: +%d rows uploaded", ckpt_n, added)
        return added

    def _task(ecli_celex):
        ecli, celex = ecli_celex
        existing = existing_by_ecli.get(ecli, set())
        return _topup_one_ecli(
            ecli, celex, existing,
            work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, sc): sc for sc in sparse}
        for fut in as_completed(futures):
            processed += 1
            try:
                rows = fut.result()
                pending_rows.extend(rows)
                total_generated += len(rows)
            except Exception as exc:
                failures += 1
                log.warning("topup failed for %s: %s", futures[fut], exc)
            if processed % 100 == 0:
                rate = processed / (time.monotonic() - start)
                remaining = len(sparse) - processed
                eta_min = (remaining / rate / 60) if rate else 0
                log.info(
                    "topup progress: %d/%d  (+%d rows so far, "
                    "%.1f ECLIs/s, ETA %.1f min)",
                    processed, len(sparse), total_generated,
                    rate, eta_min,
                )
            if checkpoint_every and processed % checkpoint_every == 0:
                total_added += _flush(pending_rows)
                pending_rows = []

    log.info(
        "topup network phase complete: %d rows generated, %d failures",
        total_generated, failures,
    )

    total_added += _flush(pending_rows)

    if total_added:
        # current_base is the last checkpoint; keep the documented output name
        final_path = workdir / "fulltexts.topped.parquet"
        os.replace(current_base, final_path)
        current_base = final_path

    stats = {
        "sparse_eclis": len(sparse),
        "new_rows_attempted": total_generated,
        "new_rows_added": total_added,
        "failures": failures,
    }

    if total_generated == 0:
        log.info("no new rows to add — dataset already topped up. Skipping upload.")
    elif total_added == 0:
        log.info(
            "after dedup all %d generated rows were duplicates; nothing to upload.",
            total_generated,
        )
    elif dry_run:
        log.info("DRY_RUN — %d rows written locally, nothing uploaded.", total_added)
    else:
        log.info("upload complete (%d rows across %d checkpoint(s)). "
                 "Viewer typically refreshes within ~60s.", total_added, ckpt_n)

    return stats


def run_upgrade(
    repo_id: str,
    workdir: Path,
    *,
    dry_run: bool = False,
    token: str | None = None,
    stub_ratio: float = 0.25,
    stub_min_median: int = 10_000,
    max_workers: int = 6,
    checkpoint_every: int = 0,
    local_cases: Path | None = None,
    local_fulltexts: Path | None = None,
    downloader=_download,
    uploader=_upload,
    work_uri_fn=None,
    items_fn=None,
    fanout_fn=None,
) -> dict:
    """Upgrade mode: replace stub texts (headnotes captured before the full
    CELLAR manifestation existed) with the full judgment text. Same
    checkpointed shape as :func:`run`, but flushes go through
    :func:`replace_rows_streaming` — existing (ecli, language) rows are
    superseded, not skipped. Idempotent: an upgraded row is no longer below
    the stub threshold, so re-runs shrink to a no-op.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    if not dry_run and not token:
        raise SystemExit("HUGGINGFACE_TOKEN required to upload. Set DRY_RUN=1 to skip.")

    cases_path = Path(local_cases) if local_cases else workdir / "cases.parquet"
    fulltexts_path = (
        Path(local_fulltexts) if local_fulltexts else workdir / "fulltexts.parquet"
    )
    if not local_cases:
        downloader(repo_id, "cases.parquet", cases_path)
    if not local_fulltexts:
        downloader(repo_id, "fulltexts.parquet", fulltexts_path)

    cases_df = pd.read_parquet(cases_path)

    def _first_celex(v):
        # Prefer a token without INF and strip _SUM/_INF-style suffixes —
        # the raw first token can be the summary document's CELEX (e.g.
        # "62020CJ0414_SUM;62020CJ0414"), whose work family carries only a
        # partial language set. 3,642 corpus cases have suffixed tokens.
        if pd.isna(v):
            return None
        parts = [p.strip() for p in str(v).split(";") if p.strip()]
        if not parts:
            return None
        non_inf = [p for p in parts if "INF" not in p]
        return (non_inf[0] if non_inf else parts[0]).split("_")[0]

    celex_by_ecli = {
        str(e): str(c)
        for e, c in zip(cases_df["ecli"], cases_df["celex"].map(_first_celex))
        if not pd.isna(e) and c
    }

    log.info("indexing stub texts (streaming) ← %s", fulltexts_path)
    stubs = stream_stub_index(
        fulltexts_path, ratio=stub_ratio, min_median=stub_min_median)
    work = [(e, celex_by_ecli[e], langs)
            for e, langs in stubs.items() if e in celex_by_ecli]
    n_stub_rows = sum(len(l) for _, _, l in work)
    log.info("found %d stub rows across %d ECLIs (ratio<%.2f, median>=%d)",
             n_stub_rows, len(work), stub_ratio, stub_min_median)

    if work_uri_fn is None or items_fn is None or fanout_fn is None:
        from cellar_extractor.eurlex_scraping import (
            _fetch_sector8_items_for_celex as _default_items_for_celex,
            _fanout_fulltexts_from_candidates as _default_fanout,
        )
        # The production path unions manifestations across every CELLAR
        # work sharing the CELEX (a single arbitrary work missed languages
        # — nine sampled judgments lost their English text that way). The
        # two-step seam is kept for the tests: the default work_uri_fn
        # passes the celex through as the token items_fn consumes.
        if work_uri_fn is None and items_fn is None:
            work_uri_fn = lambda celex, sector="6": celex
            items_fn = lambda celex: _default_items_for_celex(celex, sector="6")[1]
        fanout_fn = fanout_fn or _default_fanout

    pending: list = []
    total_generated = 0
    total_replaced = 0
    failures = 0
    processed = 0
    ckpt_n = 0
    start = time.monotonic()
    current_base = fulltexts_path

    def _flush(rows: list) -> int:
        nonlocal ckpt_n, current_base
        if not rows:
            return 0
        ckpt_n += 1
        out = workdir / f"fulltexts.upgrade{ckpt_n % 2}.parquet"
        replaced = replace_rows_streaming(current_base, rows, out)
        if replaced == 0:
            return 0
        current_base = out
        if dry_run:
            log.info("checkpoint %d: %d rows upgraded (DRY_RUN — not uploaded)",
                     ckpt_n, replaced)
        else:
            uploader(repo_id, out, "fulltexts.parquet", token)
            log.info("checkpoint %d: %d rows upgraded + uploaded", ckpt_n, replaced)
        return replaced

    def _task(item):
        ecli, celex, langs = item
        return _upgrade_one_ecli(
            ecli, celex, langs,
            work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_task, w): w for w in work}
        for fut in as_completed(futures):
            processed += 1
            try:
                rows = fut.result()
                pending.extend(rows)
                total_generated += len(rows)
            except Exception as exc:
                failures += 1
                log.warning("upgrade failed for %s: %s", futures[fut][0], exc)
            if processed % 100 == 0:
                rate = processed / (time.monotonic() - start)
                eta_min = ((len(work) - processed) / rate / 60) if rate else 0
                log.info("upgrade progress: %d/%d  (+%d rows, %.1f ECLIs/s, ETA %.1f min)",
                         processed, len(work), total_generated, rate, eta_min)
            if checkpoint_every and processed % checkpoint_every == 0:
                total_replaced += _flush(pending)
                pending = []

    log.info("upgrade network phase complete: %d rows generated, %d failures",
             total_generated, failures)
    total_replaced += _flush(pending)

    if total_replaced:
        final_path = workdir / "fulltexts.upgraded.parquet"
        os.replace(current_base, final_path)
        current_base = final_path

    stats = {
        "stub_eclis": len(work),
        "stub_rows": n_stub_rows,
        "rows_upgraded": total_replaced,
        "failures": failures,
    }
    if total_replaced == 0:
        log.info("no stub rows could be upgraded — nothing uploaded.")
    elif dry_run:
        log.info("DRY_RUN — %d rows upgraded locally, nothing uploaded.", total_replaced)
    else:
        log.info("upgrade complete: %d rows across %d checkpoint(s).",
                 total_replaced, ckpt_n)
    return stats


def main() -> int:
    repo_id = os.environ.get("HF_DATASET_REPO", "davidwickerhf/cjeu-opendata")
    dry_run = os.environ.get("DRY_RUN") == "1"
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    min_langs = int(os.environ.get("MIN_LANGS", "24"))
    year_threshold = int(os.environ.get("YEAR_THRESHOLD", "0"))
    max_workers = int(os.environ.get("WORKERS", "6"))
    checkpoint_every = int(os.environ.get("CHECKPOINT_EVERY", "2000"))
    local_cases = os.environ.get("LOCAL_CASES_PARQUET")
    local_fulltexts = os.environ.get("LOCAL_FULLTEXTS_PARQUET")

    mode = os.environ.get("MODE", "topup")
    with tempfile.TemporaryDirectory(prefix="hf-topup-") as tmp:
        if mode == "upgrade":
            stats = run_upgrade(
                repo_id, Path(tmp),
                dry_run=dry_run, token=token,
                stub_ratio=float(os.environ.get("STUB_RATIO", "0.25")),
                stub_min_median=int(os.environ.get("STUB_MIN_MEDIAN", "10000")),
                max_workers=max_workers,
                checkpoint_every=checkpoint_every,
                local_cases=Path(local_cases) if local_cases else None,
                local_fulltexts=Path(local_fulltexts) if local_fulltexts else None,
            )
        else:
            stats = run(
                repo_id, Path(tmp),
                dry_run=dry_run, token=token,
                min_langs=min_langs, year_threshold=year_threshold,
                max_workers=max_workers,
                checkpoint_every=checkpoint_every,
                local_cases=Path(local_cases) if local_cases else None,
                local_fulltexts=Path(local_fulltexts) if local_fulltexts else None,
            )
    log.info("done. stats: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
