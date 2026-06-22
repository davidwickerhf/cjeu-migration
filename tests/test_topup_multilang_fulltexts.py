"""Tests for scripts/topup_multilang_fulltexts.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "topup_multilang_fulltexts.py"
_spec = importlib.util.spec_from_file_location("topup_multilang_fulltexts", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore


# ---------------------------------------------------------------------------
# find_sparse_eclis
# ---------------------------------------------------------------------------


def test_find_sparse_eclis_picks_post_threshold_low_coverage_eclis():
    cases = pd.DataFrame([
        {"ecli": "OLD-1", "celex": "61964CJ0006", "date_publication": "1964-07-15"},
        {"ecli": "NEW-1", "celex": "62020CJ0001", "date_publication": "2020-01-15"},
        {"ecli": "NEW-2", "celex": "62020CJ0002", "date_publication": "2020-05-15"},
        {"ecli": "NEW-3", "celex": "62024CJ0099", "date_publication": "2024-02-22"},
    ])
    fulltexts = pd.DataFrame([
        # OLD-1 has 7 languages (pre-2001, well covered)
        *[{"ecli": "OLD-1", "text_language": lang}
          for lang in ["EN", "FR", "DE", "IT", "NL", "ES", "PT"]],
        # NEW-1 has 2 languages (sparse — should be picked up)
        {"ecli": "NEW-1", "text_language": "EN"},
        {"ecli": "NEW-1", "text_language": "FR"},
        # NEW-2 has 5 languages (at threshold — NOT picked, threshold is <5)
        *[{"ecli": "NEW-2", "text_language": lang}
          for lang in ["EN", "FR", "DE", "IT", "NL"]],
        # NEW-3 has 1 language (sparse — should be picked up)
        {"ecli": "NEW-3", "text_language": "FR"},
    ])
    out = mod.find_sparse_eclis(cases, fulltexts, min_langs=5, year_threshold=2001)
    eclis = {e for e, c in out}
    assert eclis == {"NEW-1", "NEW-3"}


def test_find_sparse_eclis_skips_pre_threshold_regardless_of_coverage():
    cases = pd.DataFrame([
        {"ecli": "OLD-LOW", "celex": "61964CJ0006", "date_publication": "1964-07-15"},
    ])
    # OLD-LOW has only 1 language — but it's pre-threshold, so skipped.
    fulltexts = pd.DataFrame([{"ecli": "OLD-LOW", "text_language": "EN"}])
    out = mod.find_sparse_eclis(cases, fulltexts, min_langs=10, year_threshold=2001)
    assert out == []


def test_find_sparse_eclis_handles_multi_cell_celex():
    """Some ECLIs have multi-cell celex like 62019CJ0793;62019CJ0793_RES.
    The script should use the first token as the SPARQL key."""
    cases = pd.DataFrame([
        {"ecli": "X", "celex": "62022CJ0050;62022CJ0050_RES",
         "date_publication": "2022-06-15"},
    ])
    fulltexts = pd.DataFrame([{"ecli": "X", "text_language": "FR"}])
    out = mod.find_sparse_eclis(cases, fulltexts, min_langs=5, year_threshold=2001)
    assert out == [("X", "62022CJ0050")]


def test_find_sparse_eclis_skips_rows_with_unparseable_date():
    cases = pd.DataFrame([
        {"ecli": "X", "celex": "62020CJ0001", "date_publication": "not-a-date"},
    ])
    fulltexts = pd.DataFrame([{"ecli": "X", "text_language": "EN"}])
    out = mod.find_sparse_eclis(cases, fulltexts, min_langs=5, year_threshold=2001)
    assert out == []


def test_find_sparse_eclis_empty_inputs():
    assert mod.find_sparse_eclis(pd.DataFrame(), pd.DataFrame(),
                                 min_langs=5, year_threshold=2001) == []


def test_default_min_langs_is_24_to_cover_all_eu_official_languages():
    """Lock the script-level default so a future edit can't silently lower
    it back to 5 (the original choice that missed multi-lang back-fills
    for cases sitting at 5-23 languages). The fix here was to bump the
    default to 24 — the current number of official EU languages — so
    every case below max-EU-language-coverage gets re-probed."""
    import inspect
    # find_sparse_eclis takes min_langs as a required kw-only arg; the
    # defaults live on the higher-level helpers and on main()'s env
    # fallback. Check the three places we set it:
    for fn_name in ("topup_dataset", "run"):
        sig = inspect.signature(getattr(mod, fn_name))
        assert sig.parameters["min_langs"].default == 24, (
            f"{fn_name} default min_langs is "
            f"{sig.parameters['min_langs'].default}, expected 24"
        )
    # And the env-var fallback in main() should also default to "24"
    src = inspect.getsource(mod.main)
    assert 'MIN_LANGS", "24"' in src, (
        "main() env-var fallback for MIN_LANGS is not '24'"
    )


def test_default_year_threshold_is_0_to_cover_all_decades():
    """Lock the script-level year_threshold default at 0 so a future edit
    can't silently re-introduce the year=2001 filter that previously
    excluded pre-2001 cases from the multi-lang back-fill. The earlier
    2001 default was based on the (untested) assumption that pre-2001
    cases were already at CELLAR's available maximum via the extractor's
    sector-6 fallback — which we cannot verify without actually probing.
    Default of 0 covers every decade; cases already at era-max
    probe-and-skip cheaply."""
    import inspect
    for fn_name in ("topup_dataset", "run"):
        sig = inspect.signature(getattr(mod, fn_name))
        assert sig.parameters["year_threshold"].default == 0, (
            f"{fn_name} default year_threshold is "
            f"{sig.parameters['year_threshold'].default}, expected 0"
        )
    src = inspect.getsource(mod.main)
    assert 'YEAR_THRESHOLD", "0"' in src, (
        "main() env-var fallback for YEAR_THRESHOLD is not '0'"
    )


def test_find_sparse_eclis_picks_up_modern_cases_below_24_langs():
    """The Cupriak-Trojan failure mode: a modern case with exactly 5
    languages should be picked up when min_langs=24 (it was skipped under
    the old min_langs=5 default)."""
    cases = pd.DataFrame([
        {"ecli": "RECENT-5LANG", "celex": "62023CJ0713",
         "date_publication": "2025-11-25"},
    ])
    fulltexts = pd.DataFrame([
        {"ecli": "RECENT-5LANG", "text_language": l}
        for l in ["IT", "CS", "FR", "DA", "HU"]
    ])
    # Old behaviour: with min_langs=5, this case was skipped.
    assert mod.find_sparse_eclis(cases, fulltexts,
                                 min_langs=5, year_threshold=2001) == []
    # New behaviour: with min_langs=24, this case IS picked up for re-fetch.
    out = mod.find_sparse_eclis(cases, fulltexts,
                                min_langs=24, year_threshold=2001)
    assert [e for e, _ in out] == ["RECENT-5LANG"]


# ---------------------------------------------------------------------------
# merge_new_rows
# ---------------------------------------------------------------------------


def test_merge_new_rows_skips_existing_ecli_lang_pairs():
    fulltexts = pd.DataFrame([
        {"ecli": "A", "text_language": "EN", "text": "old en"},
        {"ecli": "A", "text_language": "FR", "text": "old fr"},
    ])
    new_rows = [
        {"ecli": "A", "text_language": "EN", "text": "new en"},   # duplicate — skip
        {"ecli": "A", "text_language": "DE", "text": "new de"},   # missing — add
        {"ecli": "A", "text_language": "IT", "text": "new it"},   # missing — add
    ]
    merged, added = mod.merge_new_rows(fulltexts, new_rows)
    assert added == 2
    assert len(merged) == 4
    # Existing rows must remain unchanged
    en_row = merged[(merged["ecli"] == "A") & (merged["text_language"] == "EN")].iloc[0]
    assert en_row["text"] == "old en"


def test_merge_new_rows_empty_new_rows_is_noop():
    fulltexts = pd.DataFrame([
        {"ecli": "A", "text_language": "EN", "text": "x"},
    ])
    merged, added = mod.merge_new_rows(fulltexts, [])
    assert added == 0
    assert len(merged) == 1


def test_merge_new_rows_case_insensitive_language_dedup():
    """text_language comes in mixed case ('EN' vs 'en'). The dedup key
    must normalise so we don't end up with duplicate rows."""
    fulltexts = pd.DataFrame([
        {"ecli": "A", "text_language": "en", "text": "old"},
    ])
    new_rows = [{"ecli": "A", "text_language": "EN", "text": "new"}]
    _, added = mod.merge_new_rows(fulltexts, new_rows)
    assert added == 0


# ---------------------------------------------------------------------------
# _topup_one_ecli with stubs
# ---------------------------------------------------------------------------


def _make_stubs(work_uri="http://cellar/u", langs=("EN", "FR", "DE")):
    def work_uri_fn(celex, sector="6"):
        return work_uri
    def items_fn(uri):
        return [{"item_url": f"http://x/{l}", "format": "xhtml", "language": l}
                for l in langs]
    def fanout_fn(candidates, source_label):
        return [
            {"text": f"body in {c['language']}",
             "text_source": source_label,
             "text_language": c["language"],
             "text_format": "xhtml"}
            for c in candidates
        ]
    return work_uri_fn, items_fn, fanout_fn


def test_topup_one_ecli_returns_missing_languages():
    work_uri_fn, items_fn, fanout_fn = _make_stubs(
        langs=("EN", "FR", "DE", "IT", "NL")
    )
    existing = {"EN", "FR"}
    rows = mod._topup_one_ecli(
        "ECLI:X", "62020CJ0001", existing,
        work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
    )
    langs = {r["text_language"] for r in rows}
    assert langs == {"DE", "IT", "NL"}
    for r in rows:
        assert r["ecli"] == "ECLI:X"
        assert r["celex"] == "62020CJ0001"
        assert r["text_source"] == "CELLAR_ITEM"
        assert r["__source_window"] == "topup_v2_multilang"


def test_topup_one_ecli_noop_when_all_languages_already_present():
    work_uri_fn, items_fn, fanout_fn = _make_stubs(langs=("EN", "FR"))
    rows = mod._topup_one_ecli(
        "ECLI:X", "62020CJ0001", {"EN", "FR"},
        work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
    )
    assert rows == []


def test_topup_one_ecli_returns_empty_when_work_uri_missing():
    rows = mod._topup_one_ecli(
        "ECLI:X", "62020CJ0001", set(),
        work_uri_fn=lambda celex, sector="6": "",
        items_fn=lambda uri: pytest.fail("items_fn should not be called"),
        fanout_fn=lambda c, source_label: pytest.fail("fanout should not be called"),
    )
    assert rows == []


def test_topup_one_ecli_swallows_exceptions_at_each_step():
    """A SPARQL timeout or schema-drift error must NOT crash the worker —
    the topup is best-effort per ECLI."""

    def boom(*args, **kwargs):
        raise RuntimeError("simulated SPARQL outage")

    rows = mod._topup_one_ecli(
        "ECLI:X", "62020CJ0001", set(),
        work_uri_fn=boom, items_fn=None, fanout_fn=None,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# topup_dataset end-to-end with stubs
# ---------------------------------------------------------------------------


def test_topup_dataset_processes_sparse_eclis_in_parallel():
    cases = pd.DataFrame([
        {"ecli": "ECLI:OLD", "celex": "61964CJ0001", "date_publication": "1964-07-15"},
        {"ecli": "ECLI:NEW1", "celex": "62020CJ0001", "date_publication": "2020-01-15"},
        {"ecli": "ECLI:NEW2", "celex": "62020CJ0002", "date_publication": "2020-02-15"},
    ])
    fulltexts = pd.DataFrame([
        # OLD already has 7 langs — won't be touched
        *[{"ecli": "ECLI:OLD", "text_language": l, "text": "x"}
          for l in ["EN","FR","DE","IT","NL","ES","PT"]],
        # NEW1 has 2 langs — will get supplemented
        {"ecli": "ECLI:NEW1", "text_language": "EN", "text": "en"},
        {"ecli": "ECLI:NEW1", "text_language": "FR", "text": "fr"},
        # NEW2 has 1 lang — will get supplemented
        {"ecli": "ECLI:NEW2", "text_language": "FR", "text": "fr"},
    ])

    work_uri_fn, items_fn, fanout_fn = _make_stubs(
        langs=("EN", "FR", "DE", "IT", "NL", "ES", "PT", "PL")
    )

    updated, stats = mod.topup_dataset(
        cases, fulltexts,
        min_langs=5, year_threshold=2001, max_workers=2,
        work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
    )

    assert stats["sparse_eclis"] == 2
    # NEW1: had EN, FR. Adds: DE, IT, NL, ES, PT, PL = 6 new
    # NEW2: had FR.       Adds: EN, DE, IT, NL, ES, PT, PL = 7 new
    assert stats["new_rows_added"] == 13
    assert len(updated) == len(fulltexts) + 13

    # Verify OLD wasn't touched
    old_rows = updated[updated["ecli"] == "ECLI:OLD"]
    assert len(old_rows) == 7

    # Verify NEW1 now has 8 langs
    new1_langs = set(updated[updated["ecli"] == "ECLI:NEW1"]["text_language"].str.upper())
    assert new1_langs == {"EN","FR","DE","IT","NL","ES","PT","PL"}


def test_topup_dataset_idempotent_on_already_topped_up_data():
    """Running topup_dataset twice on the same data should add nothing the
    second time — the (ecli, lang) pairs are already present."""
    cases = pd.DataFrame([
        {"ecli": "ECLI:NEW", "celex": "62020CJ0001", "date_publication": "2020-01-01"},
    ])
    fulltexts = pd.DataFrame([
        {"ecli": "ECLI:NEW", "text_language": l, "text": l}
        for l in ["EN","FR","DE","IT","NL","ES","PT"]
    ])
    work_uri_fn, items_fn, fanout_fn = _make_stubs(
        langs=("EN","FR","DE","IT","NL","ES","PT")
    )
    updated, stats = mod.topup_dataset(
        cases, fulltexts,
        min_langs=5, year_threshold=2001, max_workers=1,
        work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
    )
    # 7 langs already, threshold is 5 — find_sparse_eclis filters it out.
    assert stats["sparse_eclis"] == 0
    assert stats["new_rows_added"] == 0
    assert len(updated) == 7


# ---------------------------------------------------------------------------
# Streaming helpers — used by run() to avoid loading the text column
# ---------------------------------------------------------------------------


def test_stream_existing_langs_by_ecli_returns_per_ecli_set(tmp_path):
    """Build a tiny parquet, stream-index it, confirm the dict matches
    what we'd get from a pandas groupby."""
    ft = pd.DataFrame([
        {"ecli": "A", "text_language": "EN", "text": "long..."},
        {"ecli": "A", "text_language": "FR", "text": "long..."},
        {"ecli": "A", "text_language": "en", "text": "case-insensitive merge"},
        {"ecli": "B", "text_language": "DE", "text": "x"},
        {"ecli": "C", "text_language": "", "text": "empty-lang row — skip"},
        {"ecli": "",  "text_language": "IT", "text": "empty-ecli row — skip"},
    ])
    p = tmp_path / "ft.parquet"
    ft.to_parquet(p, index=False)
    idx = mod.stream_existing_langs_by_ecli(p)
    assert idx == {"A": {"EN", "FR"}, "B": {"DE"}}


def test_find_sparse_eclis_from_index_matches_in_memory_version():
    """The streaming variant must produce identical output to the
    in-memory variant given equivalent inputs."""
    cases = pd.DataFrame([
        {"ecli": "OLD", "celex": "61964CJ0001", "date_publication": "1964-07-15"},
        {"ecli": "NEW-low", "celex": "62020CJ0001", "date_publication": "2020-01-15"},
        {"ecli": "NEW-full", "celex": "62020CJ0002", "date_publication": "2020-02-15"},
    ])
    fulltexts = pd.DataFrame([
        *[{"ecli": "OLD", "text_language": l} for l in ["EN", "FR", "DE"]],
        *[{"ecli": "NEW-low", "text_language": l} for l in ["EN", "FR"]],
        *[{"ecli": "NEW-full", "text_language": l}
          for l in ["EN", "FR", "DE", "IT", "NL", "ES", "PT"]],
    ])
    idx = {"OLD": {"EN", "FR", "DE"},
           "NEW-low": {"EN", "FR"},
           "NEW-full": {"EN", "FR", "DE", "IT", "NL", "ES", "PT"}}

    in_memory = mod.find_sparse_eclis(
        cases, fulltexts, min_langs=5, year_threshold=2001
    )
    streamed = mod.find_sparse_eclis_from_index(
        cases, idx, min_langs=5, year_threshold=2001
    )
    assert in_memory == streamed
    assert {e for e, _ in streamed} == {"NEW-low"}


def test_append_new_rows_streaming_dedups_and_writes_zstd(tmp_path):
    """Round-trip: write a parquet, append new rows, confirm the result
    has the dedup applied + viewer-friendly layout."""
    orig = pd.DataFrame([
        {"ecli": "A", "text_language": "EN", "text": "old en",
         "celex": "62020CJ0001", "text_source": "INFOCURIA_BLOB_HTML",
         "text_format": "html", "missing_reasons": ""},
        {"ecli": "A", "text_language": "FR", "text": "old fr",
         "celex": "62020CJ0001", "text_source": "CELLAR_ITEM",
         "text_format": "xhtml", "missing_reasons": ""},
    ])
    orig_path = tmp_path / "orig.parquet"
    orig.to_parquet(orig_path, index=False)
    new_rows = [
        {"ecli": "A", "text_language": "EN", "text": "duplicate"},   # skip
        {"ecli": "A", "text_language": "DE", "text": "new de",
         "celex": "62020CJ0001", "text_source": "CELLAR_ITEM",
         "text_format": "xhtml", "missing_reasons": "",
         "__source_window": "topup_v2_multilang"},
        {"ecli": "A", "text_language": "IT", "text": "new it",
         "celex": "62020CJ0001", "text_source": "CELLAR_ITEM",
         "text_format": "xhtml", "missing_reasons": "",
         "__source_window": "topup_v2_multilang"},
    ]
    out_path = tmp_path / "out.parquet"
    added = mod.append_new_rows_streaming(orig_path, new_rows, out_path)
    assert added == 2

    df = pd.read_parquet(out_path)
    assert len(df) == 4
    assert set(df["text_language"]) == {"EN", "FR", "DE", "IT"}
    # Original EN row preserved as-is
    en = df[df["text_language"] == "EN"].iloc[0]
    assert en["text"] == "old en"
    # New schema column (__source_window) was added; old rows have NaN/None there
    assert "__source_window" in df.columns
    # Viewer-friendly layout
    pf = pq.ParquetFile(out_path)
    col0 = pf.metadata.row_group(0).column(0)
    assert col0.has_offset_index
    assert col0.compression.lower() == "zstd"


def test_append_new_rows_streaming_noop_for_empty_input(tmp_path):
    orig = pd.DataFrame([{"ecli": "A", "text_language": "EN", "text": "x"}])
    orig_path = tmp_path / "orig.parquet"
    orig.to_parquet(orig_path, index=False)
    added = mod.append_new_rows_streaming(orig_path, [], tmp_path / "out.parquet")
    assert added == 0


def test_append_new_rows_streaming_noop_when_all_duplicates(tmp_path):
    orig = pd.DataFrame([
        {"ecli": "A", "text_language": "EN", "text": "x"},
        {"ecli": "A", "text_language": "FR", "text": "y"},
    ])
    orig_path = tmp_path / "orig.parquet"
    orig.to_parquet(orig_path, index=False)
    new_rows = [
        {"ecli": "A", "text_language": "EN", "text": "duplicate"},
        {"ecli": "A", "text_language": "fr", "text": "case-insensitive dup"},
    ]
    added = mod.append_new_rows_streaming(orig_path, new_rows, tmp_path / "out.parquet")
    assert added == 0


# ---------------------------------------------------------------------------
# run() end-to-end
# ---------------------------------------------------------------------------


def _write_inputs(tmp_path: Path):
    cases = pd.DataFrame([
        {"ecli": "ECLI:OLD", "celex": "61964CJ0001", "sector": "6",
         "date_publication": "1964-07-15"},
        {"ecli": "ECLI:NEW", "celex": "62020CJ0001", "sector": "6",
         "date_publication": "2020-01-15"},
    ])
    fulltexts = pd.DataFrame([
        *[{"ecli": "ECLI:OLD", "celex": "61964CJ0001", "text": "x",
           "text_source": "CELLAR_ITEM", "text_language": l, "text_format": "xhtml",
           "missing_reasons": ""}
          for l in ["EN","FR","DE","IT","NL","ES","PT"]],
        {"ecli": "ECLI:NEW", "celex": "62020CJ0001", "text": "en",
         "text_source": "INFOCURIA_BLOB_HTML", "text_language": "EN",
         "text_format": "html", "missing_reasons": ""},
    ])
    cpath = tmp_path / "cases.parquet"
    fpath = tmp_path / "fulltexts.parquet"
    cases.to_parquet(cpath, index=False)
    fulltexts.to_parquet(fpath, index=False)
    return cpath, fpath


def test_run_dry_run_writes_topped_parquet_locally(tmp_path):
    cpath, fpath = _write_inputs(tmp_path)
    work_uri_fn, items_fn, fanout_fn = _make_stubs(
        langs=("EN","FR","DE","IT","NL")
    )
    stats = mod.run(
        repo_id="example/x",
        workdir=tmp_path / "work",
        dry_run=True,
        local_cases=cpath, local_fulltexts=fpath,
        min_langs=5, year_threshold=2001, max_workers=1,
        downloader=lambda *a, **kw: pytest.fail("downloader called with local files"),
        uploader=lambda *a, **kw: pytest.fail("uploader called in dry-run"),
        work_uri_fn=work_uri_fn, items_fn=items_fn, fanout_fn=fanout_fn,
    )
    assert stats["sparse_eclis"] == 1
    out = tmp_path / "work" / "fulltexts.topped.parquet"
    assert out.exists()
    df = pd.read_parquet(out)
    # OLD untouched (7 langs); NEW grows from 1 to 5
    assert (df["ecli"] == "ECLI:OLD").sum() == 7
    assert (df["ecli"] == "ECLI:NEW").sum() == 5
    # New rows have the topup provenance
    topped = df[df["__source_window"] == "topup_v2_multilang"]
    assert len(topped) == 4   # 5 langs needed, 1 already had
    assert set(topped["text_language"]) == {"FR","DE","IT","NL"}
    # Viewer-friendly layout
    pf = pq.ParquetFile(out)
    col0 = pf.metadata.row_group(0).column(0)
    assert col0.has_offset_index
    assert col0.compression.lower() == "zstd"


def test_run_requires_token_when_uploading(tmp_path):
    cpath, fpath = _write_inputs(tmp_path)
    with pytest.raises(SystemExit, match="HUGGINGFACE_TOKEN"):
        mod.run(
            repo_id="example/x",
            workdir=tmp_path / "work",
            dry_run=False, token=None,
            local_cases=cpath, local_fulltexts=fpath,
            work_uri_fn=lambda *a, **kw: "",
            items_fn=lambda *a, **kw: [],
            fanout_fn=lambda *a, **kw: [],
            uploader=lambda *a, **kw: None,
        )


def test_run_skips_upload_when_no_new_rows_to_add(tmp_path):
    cpath, fpath = _write_inputs(tmp_path)
    upload_called = []
    stats = mod.run(
        repo_id="example/x",
        workdir=tmp_path / "work",
        dry_run=False, token="dummy-token",
        local_cases=cpath, local_fulltexts=fpath,
        # Stub: CELLAR has only languages NEW already has.
        work_uri_fn=lambda celex, sector="6": "http://cellar/u",
        items_fn=lambda uri: [{"item_url": "u", "format": "xhtml", "language": "EN"}],
        fanout_fn=lambda candidates, source_label: [
            {"text": "en", "text_source": source_label,
             "text_language": "EN", "text_format": "xhtml"}
        ],
        uploader=lambda *a, **kw: upload_called.append(True),
    )
    assert stats["new_rows_added"] == 0
    assert upload_called == []
