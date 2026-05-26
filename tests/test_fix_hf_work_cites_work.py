"""Tests for scripts/fix_hf_work_cites_work.py.

All tests are offline — the script is structured so the SPARQL resolver,
HF downloader, and HF uploader can be replaced with stubs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest


# Load the script as a module — it lives in scripts/, not the package.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fix_hf_work_cites_work.py"
_spec = importlib.util.spec_from_file_location("fix_hf_work_cites_work", _SCRIPT_PATH)
fix_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix_mod)  # type: ignore


# ---------------------------------------------------------------------------
# collect_unique_uris
# ---------------------------------------------------------------------------


def test_collect_unique_uris_dedups_and_skips_celex_tokens():
    s = pd.Series([
        "http://a;http://b;http://a",  # http://a dup, both URIs
        "31994R0040;62019CJ0668",       # CELEX-form, skip
        "",                              # empty
        None,                            # NaN
        "http://c;31999R0001",          # mixed: pick up http://c only
    ])
    out = fix_mod.collect_unique_uris(s)
    assert out == ["http://a", "http://b", "http://c"]


def test_collect_unique_uris_empty_series():
    assert fix_mod.collect_unique_uris(pd.Series([], dtype="object")) == []


def test_collect_unique_uris_handles_pure_celex_input():
    """A column that's already been resolved should yield no URIs to fetch."""
    s = pd.Series(["X;Y", "Z", ""])
    assert fix_mod.collect_unique_uris(s) == []


# ---------------------------------------------------------------------------
# rewrite_column
# ---------------------------------------------------------------------------


def test_rewrite_column_replaces_uris_with_celex():
    s = pd.Series(["http://a;http://b", "http://c", ""])
    mapping = {"http://a": "A1", "http://b": "B1", "http://c": "C1"}
    new, stats = fix_mod.rewrite_column(s, mapping)
    assert list(new) == ["A1;B1", "C1", ""]
    assert stats["rows_with_uri"] == 2
    assert stats["rows_rewritten"] == 2
    assert stats["uris_seen"] == 3
    assert stats["uris_resolved"] == 3
    assert stats["uris_unresolved"] == 0


def test_rewrite_column_drops_unresolved_uris():
    s = pd.Series(["http://known;http://unknown;http://other-known"])
    mapping = {"http://known": "K", "http://other-known": "OK"}
    new, stats = fix_mod.rewrite_column(s, mapping)
    assert list(new) == ["K;OK"]
    assert stats["uris_unresolved"] == 1


def test_rewrite_column_dedups_resolved_celexes():
    """Two different URIs mapping to the same CELEX collapse to one token."""
    s = pd.Series(["http://a;http://b;http://c"])
    mapping = {"http://a": "X", "http://b": "X", "http://c": "Y"}
    new, _ = fix_mod.rewrite_column(s, mapping)
    assert list(new) == ["X;Y"]


def test_rewrite_column_preserves_already_celex_rows():
    """Idempotency — a row that's already CELEX-form is returned verbatim."""
    s = pd.Series(["A;B", "C", ""])
    new, stats = fix_mod.rewrite_column(s, {"http://anything": "Z"})
    assert list(new) == ["A;B", "C", ""]
    assert stats["rows_rewritten"] == 0
    assert stats["uris_seen"] == 0


def test_rewrite_column_handles_mixed_celex_and_uri_in_one_cell():
    """If a cell contains both an already-resolved CELEX and a raw URI,
    keep the CELEX and resolve the URI; nothing duplicated."""
    s = pd.Series(["A;http://b;A;http://a"])
    mapping = {"http://a": "A", "http://b": "B"}  # http://a maps to existing A
    new, _ = fix_mod.rewrite_column(s, mapping)
    assert list(new) == ["A;B"]  # A appears once, then B


def test_rewrite_column_normalises_empty_nan_whitespace_to_empty_string():
    """All forms of "no citations" collapse to "" in the output — matches
    the convention the rest of the dataset uses for empty multi-fields."""
    s = pd.Series([None, "", "   "])
    new, stats = fix_mod.rewrite_column(s, {})
    assert list(new) == ["", "", ""]
    assert stats["rows_with_uri"] == 0
    assert stats["rows_rewritten"] == 0


# ---------------------------------------------------------------------------
# Pipeline with stubbed resolver / downloader / uploader
# ---------------------------------------------------------------------------


def _write_input_parquet(tmp_path: Path) -> Path:
    """A tiny stand-in for the HF cases.parquet."""
    df = pd.DataFrame({
        "ecli": ["ECLI:1", "ECLI:2", "ECLI:3"],
        "celex": ["A", "B", "C"],
        "work_cites_work": [
            "http://uri/x;http://uri/y",
            "",
            "http://uri/z",
        ],
        # Untouched column to verify we preserve the rest of the schema.
        "subject_matter": ["IP", "Tax", "Competition"],
    })
    src = tmp_path / "cases.parquet"
    df.to_parquet(src, index=False)
    return src


def test_run_end_to_end_dry_run_with_stubs(tmp_path):
    """Drives the full pipeline with stubbed network: downloads, resolves,
    rewrites, and writes the fixed parquet — without uploading."""
    src = _write_input_parquet(tmp_path)
    download_calls, upload_calls, resolver_calls = [], [], []

    def fake_downloader(repo_id, filename, dest):
        download_calls.append((repo_id, filename, dest))
        # Move the prepared fixture into the expected location.
        dest.write_bytes(src.read_bytes())
        return dest

    def fake_resolver(uris):
        resolver_calls.append(list(uris))
        return {
            "http://uri/x": "X",
            "http://uri/y": "Y",
            "http://uri/z": "Z",
        }

    def fake_uploader(*args, **kwargs):  # should NOT be called in dry-run
        upload_calls.append((args, kwargs))

    stats = fix_mod.run(
        repo_id="example/test-dataset",
        workdir=tmp_path / "work",
        dry_run=True,
        token=None,
        resolver=fake_resolver,
        downloader=fake_downloader,
        uploader=fake_uploader,
    )

    assert download_calls and download_calls[0][1] == "cases.parquet"
    assert resolver_calls == [["http://uri/x", "http://uri/y", "http://uri/z"]]
    assert upload_calls == []   # dry-run
    assert stats["rows_rewritten"] == 2
    assert stats["uris_resolved"] == 3

    # Final parquet on disk has CELEX-form work_cites_work and the new layout.
    fixed = tmp_path / "work" / "cases.fixed.parquet"
    out = pd.read_parquet(fixed)
    assert list(out["work_cites_work"]) == ["X;Y", "", "Z"]
    # Untouched columns survive.
    assert list(out["subject_matter"]) == ["IP", "Tax", "Competition"]
    # Viewer-friendly layout — page index + zstd + multiple-or-one row groups.
    pf = pq.ParquetFile(fixed)
    first = pf.metadata.row_group(0).column(0)
    assert first.has_offset_index
    assert first.compression.lower() == "zstd"


def test_run_uploads_when_not_dry_run(tmp_path):
    src = _write_input_parquet(tmp_path)

    def fake_downloader(repo_id, filename, dest):
        dest.write_bytes(src.read_bytes())
        return dest

    upload_args = {}

    def fake_uploader(repo_id, local_path, repo_filename, token):
        upload_args["repo_id"] = repo_id
        upload_args["local_path"] = local_path
        upload_args["repo_filename"] = repo_filename
        upload_args["token"] = token

    fix_mod.run(
        repo_id="example/test-dataset",
        workdir=tmp_path / "work",
        dry_run=False,
        token="hf-secret",
        resolver=lambda uris: {u: f"CELEX-{i}" for i, u in enumerate(uris)},
        downloader=fake_downloader,
        uploader=fake_uploader,
    )
    assert upload_args["repo_id"] == "example/test-dataset"
    assert upload_args["repo_filename"] == "cases.parquet"
    assert upload_args["token"] == "hf-secret"
    assert upload_args["local_path"].name == "cases.fixed.parquet"


def test_run_requires_token_when_uploading(tmp_path):
    src = _write_input_parquet(tmp_path)

    def fake_downloader(repo_id, filename, dest):
        dest.write_bytes(src.read_bytes())
        return dest

    with pytest.raises(SystemExit, match="HUGGINGFACE_TOKEN"):
        fix_mod.run(
            repo_id="example/test-dataset",
            workdir=tmp_path / "work",
            dry_run=False,
            token=None,
            resolver=lambda uris: {u: "X" for u in uris},
            downloader=fake_downloader,
            uploader=lambda *a, **kw: None,
        )


def test_run_is_noop_when_column_already_celex(tmp_path):
    """Re-running on an already-fixed dataset should resolve nothing."""
    # All-CELEX input: no URIs at all.
    df = pd.DataFrame({
        "ecli": ["E1"],
        "celex": ["A"],
        "work_cites_work": ["X;Y;Z"],
    })
    src = tmp_path / "cases.parquet"
    df.to_parquet(src, index=False)

    resolver_called = []

    def fake_downloader(repo_id, filename, dest):
        dest.write_bytes(src.read_bytes())
        return dest

    def fake_resolver(uris):
        resolver_called.append(uris)
        return {}

    stats = fix_mod.run(
        repo_id="example/test",
        workdir=tmp_path / "work",
        dry_run=True,
        resolver=fake_resolver,
        downloader=fake_downloader,
        uploader=lambda *a, **kw: None,
    )
    # Short-circuited: resolver never invoked because there's nothing to resolve.
    assert resolver_called == []
    assert stats["rows_rewritten"] == 0


def test_run_bails_when_column_missing(tmp_path):
    """A parquet without the column should exit loudly rather than silently."""
    df = pd.DataFrame({"ecli": ["A"], "celex": ["B"]})
    src = tmp_path / "cases.parquet"
    df.to_parquet(src, index=False)

    def fake_downloader(repo_id, filename, dest):
        dest.write_bytes(src.read_bytes())
        return dest

    with pytest.raises(SystemExit, match="work_cites_work"):
        fix_mod.run(
            repo_id="x/y",
            workdir=tmp_path / "work",
            dry_run=True,
            resolver=lambda *_: {},
            downloader=fake_downloader,
            uploader=lambda *a, **kw: None,
        )
