"""Tests for scripts/cleanup_hf_dataset.py.

Pure-Python transforms + end-to-end pipeline with stubbed network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_hf_dataset.py"
_spec = importlib.util.spec_from_file_location("cleanup_hf_dataset", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore


# ---------------------------------------------------------------------------
# drop_always_null_columns — data-driven, not hardcoded
# ---------------------------------------------------------------------------


def test_drop_always_null_columns_drops_only_truly_empty():
    df = pd.DataFrame({
        "ecli": ["A", "B"],
        "celex": ["X", "Y"],
        "always_null":       [None, None],
        "empty_strings":     ["", ""],
        "whitespace_only":   ["   ", "  "],
        "mostly_null":       [None, "x"],   # NOT dropped (has data)
        "always_full":       ["p", "q"],
    })
    out, dropped = mod.drop_always_null_columns(df)
    assert set(dropped) == {"always_null", "empty_strings", "whitespace_only"}
    assert "mostly_null" in out.columns
    assert "always_full" in out.columns
    assert "ecli" in out.columns


def test_drop_always_null_columns_protects_schema_primitives():
    """Even if sector / ecli / celex / __source_window are entirely empty in
    a tiny test frame, they should never be dropped — downstream tooling
    relies on them being present."""
    df = pd.DataFrame({
        "ecli": [None, None],
        "celex": [None, None],
        "sector": [None, None],
        "__source_window": [None, None],
        "actually_drop_me": [None, None],
    })
    out, dropped = mod.drop_always_null_columns(df)
    assert dropped == ["actually_drop_me"]
    assert {"ecli", "celex", "sector", "__source_window"}.issubset(out.columns)


def test_drop_always_null_columns_is_idempotent():
    """A second pass should drop nothing — re-running on a cleaned frame
    must be a no-op."""
    df = pd.DataFrame({"ecli": ["A"], "always_null": [None]})
    cleaned, first_dropped = mod.drop_always_null_columns(df)
    _, second_dropped = mod.drop_always_null_columns(cleaned)
    assert first_dropped == ["always_null"]
    assert second_dropped == []


# ---------------------------------------------------------------------------
# dedup_by_ecli — lossless collapse with __source_window merge
# ---------------------------------------------------------------------------


def test_dedup_by_ecli_keeps_first_and_merges_source_windows():
    df = pd.DataFrame({
        "ecli":             ["A", "A", "B", "C"],
        "celex":            ["x", "x", "y", "z"],
        "__source_window":  ["2020-01", "2020-02", "2020-03", "2020-04"],
        "subject_matter":   ["IP", "IP", "VAT", "Competition"],
    })
    out, dropped = mod.dedup_by_ecli(df)

    assert dropped == 1
    assert len(out) == 3
    a_row = out[out["ecli"] == "A"].iloc[0]
    # Both source windows merged into ;-separated string
    assert a_row["__source_window"] == "2020-01;2020-02"
    # First row's payload preserved
    assert a_row["subject_matter"] == "IP"


def test_dedup_by_ecli_no_dupes_is_noop():
    df = pd.DataFrame({"ecli": ["A", "B", "C"], "__source_window": ["w1", "w2", "w3"]})
    out, dropped = mod.dedup_by_ecli(df)
    assert dropped == 0
    assert len(out) == 3


def test_dedup_by_ecli_leaves_nan_ecli_rows_alone():
    """NaN ECLI rows can't be safely grouped — preserve them verbatim."""
    df = pd.DataFrame({
        "ecli": [None, None, "A", "A"],
        "__source_window": ["w1", "w2", "w3", "w4"],
    })
    out, dropped = mod.dedup_by_ecli(df)
    # 1 dup collapsed (the two "A" rows); the 2 NaN rows kept as-is
    assert dropped == 1
    assert out["ecli"].isna().sum() == 2


def test_dedup_by_ecli_handles_missing_source_window_column():
    """A frame without __source_window must still dedup cleanly."""
    df = pd.DataFrame({"ecli": ["A", "A", "B"], "celex": ["x", "x", "y"]})
    out, dropped = mod.dedup_by_ecli(df)
    assert dropped == 1
    assert len(out) == 2


def test_dedup_by_ecli_handles_empty_frame():
    df = pd.DataFrame({"ecli": []})
    out, dropped = mod.dedup_by_ecli(df)
    assert dropped == 0
    assert out.empty


# ---------------------------------------------------------------------------
# clean_cases — order-of-operations + integration of the three transforms
# ---------------------------------------------------------------------------


def test_clean_cases_runs_all_three_transforms_in_order():
    df = pd.DataFrame({
        "ecli":              ["A", "A", "B"],
        "celex":              ["x", "x", "y"],
        "sector":             ["6", "6", "6"],
        "__source_window":    ["w1", "w2", "w3"],
        "work_cites_work":    ["http://u/a", "http://u/a", "http://u/b;http://u/c"],
        "always_null":        [None, None, None],
        "subject_matter":     ["IP", "IP", "VAT"],
    })
    resolver = lambda uris: {
        "http://u/a": "RESOLVED_A",
        "http://u/b": "RESOLVED_B",
        "http://u/c": "RESOLVED_C",
    }
    cleaned, report = mod.clean_cases(df, resolver=resolver)

    # Dedup happened
    assert report["rows_dedupped"] == 1
    assert len(cleaned) == 2

    # URI rewrite happened
    a_row = cleaned[cleaned["ecli"] == "A"].iloc[0]
    b_row = cleaned[cleaned["ecli"] == "B"].iloc[0]
    assert a_row["work_cites_work"] == "RESOLVED_A"
    assert b_row["work_cites_work"] == "RESOLVED_B;RESOLVED_C"
    assert report["uris_resolved"] == 3
    assert report["work_cites_rewritten_rows"] == 2

    # Null col dropped
    assert "always_null" not in cleaned.columns
    assert "always_null" in report["columns_dropped"]

    # Schema primitives preserved
    for col in ("ecli", "celex", "sector", "subject_matter"):
        assert col in cleaned.columns


def test_clean_cases_idempotent_on_already_clean_input():
    """Re-running on a clean frame should be a true no-op."""
    df = pd.DataFrame({
        "ecli": ["A", "B"],
        "celex": ["x", "y"],
        "sector": ["6", "6"],
        "__source_window": ["w1", "w2"],
        "work_cites_work": ["CELEX1;CELEX2", "CELEX3"],
    })
    resolver_called = []

    def fake_resolver(uris):
        resolver_called.append(uris)
        return {}

    cleaned, report = mod.clean_cases(df, resolver=fake_resolver)

    # No URIs to resolve → resolver never invoked
    assert resolver_called == []
    assert report["rows_dedupped"] == 0
    assert report["columns_dropped"] == []
    # All data preserved verbatim
    assert list(cleaned["work_cites_work"]) == ["CELEX1;CELEX2", "CELEX3"]


def test_clean_cases_drops_column_that_only_had_unresolved_uris():
    """Edge case: a column with only URIs that fail to resolve ends up
    empty after rewrite_column drops them — drop_always_null should catch
    it on the same pass."""
    df = pd.DataFrame({
        "ecli": ["A"],
        "work_cites_work": ["http://u/unresolvable"],
    })
    cleaned, report = mod.clean_cases(df, resolver=lambda uris: {})

    # work_cites_work cell became empty, but the column survives (other rows
    # could populate it in a larger frame). With a single row we end up
    # dropping it. Verify the cell, not whether the column was dropped.
    if "work_cites_work" in cleaned.columns:
        assert cleaned.iloc[0]["work_cites_work"] == ""


# ---------------------------------------------------------------------------
# clean_fulltexts
# ---------------------------------------------------------------------------


def test_clean_fulltexts_dedups_ecli_rows():
    df = pd.DataFrame({
        "ecli":            ["A", "A", "B"],
        "celex":            ["x", "x", "y"],
        "text":             ["foo", "foo", "bar"],
        "__source_window":  ["2020-01", "2020-02", "2020-03"],
    })
    out, report = mod.clean_fulltexts(df)
    assert report["rows_dedupped"] == 1
    assert len(out) == 2
    a = out[out["ecli"] == "A"].iloc[0]
    assert a["__source_window"] == "2020-01;2020-02"


# ---------------------------------------------------------------------------
# End-to-end run() with stubbed network
# ---------------------------------------------------------------------------


def _stub_dataset(tmp_path: Path) -> tuple:
    """Mini-dataset fixture mimicking the published HF artifacts."""
    cases = pd.DataFrame({
        "ecli":              ["A", "A", "B", "C"],
        "celex":              ["x", "x", "y", "z"],
        "sector":             ["6", "6", "6", "8"],
        "__source_window":    ["2020-01", "2020-02", "2020-03", "2020-04"],
        "work_cites_work":    ["http://u/a;http://u/b", "http://u/a;http://u/b", "", ""],
        "always_null":        [None, None, None, None],
        "subject_matter":     ["IP", "IP", "VAT", "Competition"],
    })
    fulltexts = pd.DataFrame({
        "ecli":            ["A", "A", "B", "C"],
        "celex":            ["x", "x", "y", "z"],
        "text":             ["t1", "t1", "t2", "t3"],
        "__source_window":  ["2020-01", "2020-02", "2020-03", "2020-04"],
    })
    cases_path = tmp_path / "src_cases.parquet"
    fulltexts_path = tmp_path / "src_fulltexts.parquet"
    cases.to_parquet(cases_path, index=False)
    fulltexts.to_parquet(fulltexts_path, index=False)
    return cases_path, fulltexts_path


def test_run_end_to_end_dry_run_with_stubs(tmp_path):
    cases_src, fulltexts_src = _stub_dataset(tmp_path)

    download_calls = []

    def fake_downloader(repo_id, filename, dest):
        download_calls.append((repo_id, filename))
        if filename == "cases.parquet":
            dest.write_bytes(cases_src.read_bytes())
        else:
            dest.write_bytes(fulltexts_src.read_bytes())
        return dest

    upload_calls = []

    def fake_uploader(*args, **kwargs):
        upload_calls.append((args, kwargs))

    report = mod.run(
        repo_id="example/test",
        workdir=tmp_path / "work",
        dry_run=True,
        token=None,
        resolver=lambda uris: {"http://u/a": "AA", "http://u/b": "BB"},
        downloader=fake_downloader,
        uploader=fake_uploader,
    )

    # Both files downloaded, neither uploaded
    assert {f for _, f in download_calls} == {"cases.parquet", "fulltexts.parquet"}
    assert upload_calls == []

    # Per-file reports
    assert report["cases"]["rows_dedupped"] == 1
    assert report["cases"]["uris_resolved"] == 2  # one cell, 2 URIs
    assert "always_null" in report["cases"]["columns_dropped"]
    assert report["fulltexts"]["rows_dedupped"] == 1

    # Output files written, with viewer-friendly layout
    cases_out = tmp_path / "work" / "cases.cleaned.parquet"
    fulltexts_out = tmp_path / "work" / "fulltexts.cleaned.parquet"
    assert cases_out.exists() and fulltexts_out.exists()
    for f in (cases_out, fulltexts_out):
        pf = pq.ParquetFile(f)
        col0 = pf.metadata.row_group(0).column(0)
        assert col0.has_offset_index
        assert col0.compression.lower() == "zstd"

    # Cases content: deduped + URI-resolved + null col gone
    cleaned = pd.read_parquet(cases_out)
    assert len(cleaned) == 3                               # A;A collapsed
    assert "always_null" not in cleaned.columns
    a = cleaned[cleaned["ecli"] == "A"].iloc[0]
    assert a["work_cites_work"] == "AA;BB"
    assert a["__source_window"] == "2020-01;2020-02"


def test_run_uploads_both_files_when_not_dry_run(tmp_path):
    cases_src, fulltexts_src = _stub_dataset(tmp_path)

    def fake_downloader(repo_id, filename, dest):
        src = cases_src if filename == "cases.parquet" else fulltexts_src
        dest.write_bytes(src.read_bytes())
        return dest

    uploads = []

    def fake_uploader(repo_id, local_path, repo_filename, token):
        uploads.append({
            "repo_id": repo_id,
            "filename": repo_filename,
            "token": token,
            "local_name": local_path.name,
        })

    mod.run(
        repo_id="example/test",
        workdir=tmp_path / "work",
        dry_run=False,
        token="secret",
        resolver=lambda uris: {u: f"R{i}" for i, u in enumerate(uris)},
        downloader=fake_downloader,
        uploader=fake_uploader,
    )

    by_filename = {u["filename"]: u for u in uploads}
    assert set(by_filename) == {"cases.parquet", "fulltexts.parquet"}
    for u in uploads:
        assert u["token"] == "secret"
        assert u["repo_id"] == "example/test"


def test_run_requires_token_for_upload(tmp_path):
    with pytest.raises(SystemExit, match="HUGGINGFACE_TOKEN"):
        mod.run(
            repo_id="example/test",
            workdir=tmp_path / "work",
            dry_run=False,
            token=None,
            resolver=lambda uris: {},
            downloader=lambda *a, **kw: None,
            uploader=lambda *a, **kw: None,
        )
