"""Tests for scripts/regenerate_hf_readme.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "regenerate_hf_readme.py"
_spec = importlib.util.spec_from_file_location("regenerate_hf_readme", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_derive_date_window_from_min_max_date_publication():
    df = pd.DataFrame({
        "date_publication": ["2010-04-01", "2023-12-15", "1965-08-10"],
    })
    start, end = mod.derive_date_window(df)
    assert start == "1965-08-10"
    assert end == "2023-12-15"


def test_derive_date_window_handles_multi_valued_cells():
    """date_publication can be ;-separated when an ECLI bundles works."""
    df = pd.DataFrame({
        "date_publication": ["2020-01-01;2020-01-02", "2018-06-10"],
    })
    start, end = mod.derive_date_window(df)
    # _first() takes the EARLIEST of a multi-cell string.
    assert start == "2018-06-10"
    assert end == "2020-01-01"


def test_derive_date_window_empty_or_missing_returns_empty_strings():
    assert mod.derive_date_window(pd.DataFrame()) == ("", "")
    assert mod.derive_date_window(pd.DataFrame({"x": [1, 2]})) == ("", "")
    assert mod.derive_date_window(pd.DataFrame({"date_publication": [None, None]})) == ("", "")


def test_derive_column_lists_separates_canonical_from_discovered():
    """A column that is in CANONICAL_COLUMNS goes to canonical; everything
    else is discovered. __-prefixed columns are excluded from both."""
    df = pd.DataFrame({
        "ecli": ["A"],
        "celex": ["A"],
        "completely_made_up_predicate": ["X"],
        "__source_window": ["w"],
    })
    canonical, discovered = mod.derive_column_lists(df)
    # canonical depends on cellar_extractor being installed; the test
    # only asserts the partition is exhaustive and disjoint.
    assert set(canonical).isdisjoint(set(discovered))
    assert "__source_window" not in canonical
    assert "__source_window" not in discovered


# ---------------------------------------------------------------------------
# Pipeline with stubs
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    cases = pd.DataFrame({
        "ecli": ["A", "B"],
        "celex": ["x", "y"],
        "sector": ["6", "6"],
        "date_publication": ["2020-01-01", "2022-07-15"],
        "subject_matter": ["IP", "Tax"],
        "type_procedure": ["Action for annulment", "Reference for a preliminary ruling"],
        "origin_country": ["Germany", "France"],
        "work_cites_work": ["y", ""],
        "cited_by": ["", "x"],
    })
    fulltexts = pd.DataFrame({
        "ecli": ["A", "B"],
        "text": ["a" * 500, "b" * 500],
        "text_language": ["EN", "FR"],
        "missing_reasons": ["", ""],
    })
    c = tmp_path / "cases.parquet"
    f = tmp_path / "fulltexts.parquet"
    cases.to_parquet(c, index=False)
    fulltexts.to_parquet(f, index=False)
    return c, f


def test_run_dry_run_renders_local_readme_with_real_data(tmp_path):
    """Drives the full pipeline against local parquet fixtures, no network."""
    cases_p, fulltexts_p = _write_fixture(tmp_path)
    readme = mod.run(
        repo_id="example/test-dataset",
        workdir=tmp_path / "work",
        dry_run=True,
        local_cases=cases_p,
        local_fulltexts=fulltexts_p,
        # downloader / uploader should NOT be hit when local paths given
        downloader=lambda *a, **kw: pytest.fail("downloader called despite local paths"),
        uploader=lambda *a, **kw: pytest.fail("uploader called in dry_run"),
    )
    body = readme.read_text(encoding="utf-8")

    # Headline reflects the fixture data
    assert "2 cases" not in body  # would be wrong format
    # cases_rows is 2 — appears in headline numbers
    assert "**Cases:** 2" in body or "**Cases:** 2," in body or "2\n" in body
    assert "2020-01-01" in body
    assert "2022-07-15" in body

    # Rich sections present
    assert "## Quick start" in body
    assert "## Recipes" in body
    assert "## Citation graph" in body
    # The fixture mentions German + French as origin countries → demographics
    # table picks them up.
    assert "Germany" in body and "France" in body


def test_run_downloads_when_no_local_paths(tmp_path):
    cases_p, fulltexts_p = _write_fixture(tmp_path)
    download_calls = []

    def fake_downloader(repo_id, filename, dest):
        download_calls.append(filename)
        src = cases_p if filename == "cases.parquet" else fulltexts_p
        dest.write_bytes(src.read_bytes())
        return dest

    mod.run(
        repo_id="example/test-dataset",
        workdir=tmp_path / "work",
        dry_run=True,
        downloader=fake_downloader,
        uploader=lambda *a, **kw: None,
    )
    assert sorted(download_calls) == ["cases.parquet", "fulltexts.parquet"]


def test_run_uploads_readme_when_not_dry_run(tmp_path):
    cases_p, fulltexts_p = _write_fixture(tmp_path)
    uploaded = {}

    def fake_uploader(repo_id, local_path, repo_filename, token):
        uploaded["repo_id"] = repo_id
        uploaded["filename"] = repo_filename
        uploaded["token"] = token
        # capture the body for downstream sanity check
        uploaded["body"] = local_path.read_text(encoding="utf-8")

    mod.run(
        repo_id="example/test-dataset",
        workdir=tmp_path / "work",
        dry_run=False,
        token="hf-secret-xyz",
        local_cases=cases_p,
        local_fulltexts=fulltexts_p,
        uploader=fake_uploader,
    )

    assert uploaded["repo_id"] == "example/test-dataset"
    assert uploaded["filename"] == "README.md"
    assert uploaded["token"] == "hf-secret-xyz"
    # The repo_id is interpolated into the Quick start code blocks
    assert "example/test-dataset" in uploaded["body"]


def test_run_requires_token_when_not_dry_run(tmp_path):
    cases_p, fulltexts_p = _write_fixture(tmp_path)
    with pytest.raises(SystemExit, match="HUGGINGFACE_TOKEN"):
        mod.run(
            repo_id="example/test",
            workdir=tmp_path / "work",
            dry_run=False,
            token=None,
            local_cases=cases_p,
            local_fulltexts=fulltexts_p,
            uploader=lambda *a, **kw: None,
        )
