"""Tests for the window-to-parquet consolidation step."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cjeu_migration.consolidate import (
    compute_coverage_stats,
    consolidate_cases,
    consolidate_fulltexts,
    write_dataset_card,
)


# ---------------------------------------------------------------------------
# Cases consolidation
# ---------------------------------------------------------------------------


def _write_window_csv(path: Path, header: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            f.write(",".join(row) + "\n")


def test_consolidate_cases_concatenates_all_windows(tmp_path):
    win_dir = tmp_path / "windows"
    _write_window_csv(
        win_dir / "2020-01.csv",
        ["celex", "ecli", "sector"],
        [("62020CJ0001", "ECLI:EU:C:2020:1", "6")],
    )
    _write_window_csv(
        win_dir / "2020-02.csv",
        ["celex", "ecli", "sector"],
        [("62020CJ0002", "ECLI:EU:C:2020:2", "6")],
    )
    out = tmp_path / "out" / "cases.parquet"

    df = consolidate_cases(win_dir, out)

    assert out.exists()
    assert len(df) == 2
    assert set(df["celex"]) == {"62020CJ0001", "62020CJ0002"}
    assert set(df["__source_window"]) == {"2020-01", "2020-02"}


def test_consolidate_cases_handles_schema_drift(tmp_path):
    """Different windows may have different discovered columns. The union
    should be preserved with nulls in rows that didn't carry the column."""
    win_dir = tmp_path / "windows"
    _write_window_csv(
        win_dir / "2019-01.csv",
        ["celex", "ecli", "early_only_column"],
        [("62019CJ0001", "ECLI:EU:C:2019:1", "value_a")],
    )
    _write_window_csv(
        win_dir / "2020-01.csv",
        ["celex", "ecli", "late_only_column"],
        [("62020CJ0001", "ECLI:EU:C:2020:1", "value_b")],
    )
    out = tmp_path / "out" / "cases.parquet"

    df = consolidate_cases(win_dir, out)

    assert len(df) == 2
    # Union of columns from both windows is present.
    assert "early_only_column" in df.columns
    assert "late_only_column" in df.columns
    # Each row only has its own discovered column populated.
    early_row = df[df["celex"] == "62019CJ0001"].iloc[0]
    late_row = df[df["celex"] == "62020CJ0001"].iloc[0]
    assert early_row["early_only_column"] == "value_a"
    assert pd.isna(early_row["late_only_column"])
    assert late_row["late_only_column"] == "value_b"
    assert pd.isna(late_row["early_only_column"])


def test_consolidate_cases_empty_dir_writes_empty_parquet(tmp_path):
    out = tmp_path / "out" / "cases.parquet"
    df = consolidate_cases(tmp_path / "no-windows", out)
    assert out.exists()
    assert df.empty


def test_consolidate_cases_skips_unreadable_csvs(tmp_path):
    win_dir = tmp_path / "windows"
    win_dir.mkdir()
    (win_dir / "good.csv").write_text("celex,ecli\nA,B\n", encoding="utf-8")
    (win_dir / "empty.csv").write_text("", encoding="utf-8")
    out = tmp_path / "out" / "cases.parquet"
    df = consolidate_cases(win_dir, out)
    assert len(df) == 1
    assert df.iloc[0]["celex"] == "A"


# ---------------------------------------------------------------------------
# Fulltext consolidation
# ---------------------------------------------------------------------------


def test_consolidate_fulltexts_concatenates_all_windows(tmp_path):
    win_dir = tmp_path / "fulltexts"
    win_dir.mkdir()
    (win_dir / "2020-01.json").write_text(
        json.dumps(
            [
                {"celex": "62020CJ0001", "ecli": "ECLI:EU:C:2020:1", "text": "hello"},
                {"celex": "62020CJ0002", "ecli": "ECLI:EU:C:2020:2", "text": "world"},
            ]
        ),
        encoding="utf-8",
    )
    (win_dir / "2020-02.json").write_text(
        json.dumps([{"celex": "62020CJ0003", "ecli": "ECLI:EU:C:2020:3", "text": "foo"}]),
        encoding="utf-8",
    )
    out = tmp_path / "out" / "fulltexts.parquet"
    df = consolidate_fulltexts(win_dir, out)

    assert out.exists()
    assert len(df) == 3
    assert set(df["celex"]) == {"62020CJ0001", "62020CJ0002", "62020CJ0003"}
    assert set(df["__source_window"]) == {"2020-01", "2020-02"}


def test_consolidate_fulltexts_skips_malformed_files(tmp_path):
    win_dir = tmp_path / "fulltexts"
    win_dir.mkdir()
    (win_dir / "good.json").write_text(json.dumps([{"celex": "A", "text": "ok"}]))
    (win_dir / "bad.json").write_text("not json at all")
    (win_dir / "wrong-shape.json").write_text(json.dumps({"not": "a list"}))
    out = tmp_path / "out" / "fulltexts.parquet"
    df = consolidate_fulltexts(win_dir, out)
    assert len(df) == 1
    assert df.iloc[0]["celex"] == "A"


def test_consolidate_fulltexts_empty_dir(tmp_path):
    out = tmp_path / "out" / "fulltexts.parquet"
    df = consolidate_fulltexts(tmp_path / "no-fulltexts", out)
    assert out.exists()
    assert df.empty


# ---------------------------------------------------------------------------
# Viewer-friendly parquet layout (regression guards for HF dataset viewer)
# ---------------------------------------------------------------------------


def test_cases_parquet_uses_small_row_groups_for_hf_viewer(tmp_path):
    """One giant row group blows past HF's 300 MB scan cap.

    Force enough rows that the row-group cap kicks in, then check the
    resulting file has multiple row groups (random access works) and a
    page index (seek without scanning).
    """
    import pyarrow.parquet as pq

    from cjeu_migration.consolidate import CASES_ROW_GROUP_SIZE

    # 2.5× the row-group size — guaranteed to split into at least 3 groups.
    n_rows = CASES_ROW_GROUP_SIZE * 2 + 100
    win_dir = tmp_path / "windows"
    _write_window_csv(
        win_dir / "2020-01.csv",
        ["celex", "ecli", "sector"],
        [(f"62020CJ{i:04d}", f"ECLI:EU:C:2020:{i}", "6") for i in range(n_rows)],
    )
    out = tmp_path / "out" / "cases.parquet"
    consolidate_cases(win_dir, out)

    pf = pq.ParquetFile(out)
    assert pf.num_row_groups >= 3, (
        f"expected multi-row-group layout, got {pf.num_row_groups}"
    )
    # Every group should respect the configured cap.
    for i in range(pf.num_row_groups):
        rg = pf.metadata.row_group(i)
        assert rg.num_rows <= CASES_ROW_GROUP_SIZE, (
            f"row group {i} has {rg.num_rows} rows, cap is {CASES_ROW_GROUP_SIZE}"
        )
    # Page index is required for the viewer to seek without scanning.
    first_col = pf.metadata.row_group(0).column(0)
    assert first_col.has_offset_index, "page-offset index missing — HF viewer needs it"
    # zstd compression — smaller and faster than the pandas default.
    assert first_col.compression.lower() == "zstd"


def test_fulltexts_parquet_uses_small_row_groups_for_hf_viewer(tmp_path):
    import pyarrow.parquet as pq

    from cjeu_migration.consolidate import FULLTEXTS_ROW_GROUP_SIZE

    n_rows = FULLTEXTS_ROW_GROUP_SIZE * 2 + 50
    win_dir = tmp_path / "fulltexts"
    win_dir.mkdir()
    entries = [
        {"celex": f"62020CJ{i:04d}", "ecli": f"ECLI:EU:C:2020:{i}", "text": "x" * 100}
        for i in range(n_rows)
    ]
    (win_dir / "2020-01.json").write_text(json.dumps(entries), encoding="utf-8")
    out = tmp_path / "out" / "fulltexts.parquet"
    consolidate_fulltexts(win_dir, out)

    pf = pq.ParquetFile(out)
    assert pf.num_row_groups >= 3
    assert pf.metadata.row_group(0).column(0).has_offset_index
    assert pf.metadata.row_group(0).column(0).compression.lower() == "zstd"


# ---------------------------------------------------------------------------
# Dataset card
# ---------------------------------------------------------------------------


def test_write_dataset_card_includes_key_facts(tmp_path):
    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=1500,
        fulltexts_rows=1489,
        start_date="2020-01-01",
        end_date="2020-12-31",
        canonical_columns=["celex", "ecli", "summary"],
        discovered_columns=["work_part_of_dossier"],
        hf_dataset_repo="example-org/cjeu-cases",
    )
    body = out.read_text("utf-8")
    assert "license: apache-2.0" in body
    assert "1,500" in body or "1500" in body  # tolerant of formatting
    assert "1,489" in body
    assert "2020-01-01" in body and "2020-12-31" in body
    assert "example-org/cjeu-cases" in body
    assert "`celex`" in body
    assert "`work_part_of_dossier`" in body


def test_copy_fields_md_falls_back_to_github_when_not_installed(tmp_path, monkeypatch):
    """When cellar-extractor's install doesn't ship FIELDS.md (current upstream
    MANIFEST.in), the helper falls back to fetching the raw file from GitHub."""
    from unittest.mock import patch

    out = tmp_path / "FIELDS.md"

    # Force the local lookup to return nothing.
    from cjeu_migration import consolidate
    monkeypatch.setattr(consolidate, "_locate_fields_md", lambda: [])

    # Fake urllib response.
    fake_body = b"# CJEU FIELDS\n\nFetched from GitHub.\n"

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return fake_body

    with patch("urllib.request.urlopen", return_value=_FakeResponse()) as urlopen_mock:
        ok = consolidate.copy_fields_md(out)

    assert ok is True
    assert out.exists()
    assert "Fetched from GitHub" in out.read_text("utf-8")
    urlopen_mock.assert_called_once()
    called_url = urlopen_mock.call_args[0][0]
    assert "FIELDS.md" in called_url
    assert "cellar-extractor" in called_url


def test_copy_fields_md_returns_false_when_local_and_remote_both_fail(tmp_path, monkeypatch):
    """If neither the local install nor GitHub is reachable, the helper logs
    a warning and returns False — the rest of the pipeline keeps going."""
    from unittest.mock import patch

    out = tmp_path / "FIELDS.md"
    from cjeu_migration import consolidate
    monkeypatch.setattr(consolidate, "_locate_fields_md", lambda: [])

    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        ok = consolidate.copy_fields_md(out)

    assert ok is False
    assert not out.exists()


def test_write_dataset_card_handles_no_discovered_columns(tmp_path):
    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=10,
        fulltexts_rows=10,
        start_date="2020-01-01",
        end_date="2020-01-31",
        canonical_columns=["celex"],
        discovered_columns=[],
        hf_dataset_repo="example/x",
    )
    body = out.read_text("utf-8")
    assert "_(none populated)_" in body


# ---------------------------------------------------------------------------
# Coverage statistics + dataset-card Coverage section
# ---------------------------------------------------------------------------


def _make_coverage_fixture():
    """Two small frames covering decades, sectors, languages, dupes, and
    missing-reason cases — everything compute_coverage_stats touches."""
    cases = pd.DataFrame([
        # 1960s, sector 6, no fulltext (pre-CELLAR-digitisation)
        {"ecli": "ECLI:EU:C:1965:1",  "celex": "61965CJ0001", "sector": "6",
         "date_publication": "1965-04-01"},
        # 2000s, sector 6, with fulltext
        {"ecli": "ECLI:EU:C:2005:10", "celex": "62005CJ0010", "sector": "6",
         "date_publication": "2005-04-01"},
        # 2020s, sector 6, with fulltext
        {"ecli": "ECLI:EU:C:2022:50", "celex": "62022CJ0050", "sector": "6",
         "date_publication": "2022-06-15"},
        # 2020s, sector 8 (national case law), with fulltext
        {"ecli": "ECLI:DE:BVerwG:2023:1", "celex": "82023DE0001", "sector": "8",
         "date_publication": "2023-01-12"},
        # 2020s, multi-sector cell
        {"ecli": "ECLI:EU:C:2024:99", "celex": "62024CJ0099", "sector": "6;8",
         "date_publication": "2024-02-22"},
        # duplicate ECLI (same as 2022:50) — should bump dup_ecli_count
        {"ecli": "ECLI:EU:C:2022:50", "celex": "62022CJ0050", "sector": "6",
         "date_publication": "2022-06-15"},
    ])
    fulltexts = pd.DataFrame([
        # The 1965 case has no fulltext — empty body and a missing reason.
        {"ecli": "ECLI:EU:C:1965:1",  "celex": "61965CJ0001", "text": "",
         "text_language": "", "missing_reasons": "FULLTEXT_UNAVAILABLE_UPSTREAM"},
        {"ecli": "ECLI:EU:C:2005:10", "celex": "62005CJ0010", "text": "x" * 500,
         "text_language": "FR", "missing_reasons": ""},
        {"ecli": "ECLI:EU:C:2022:50", "celex": "62022CJ0050", "text": "y" * 5000,
         "text_language": "EN", "missing_reasons": ""},
        {"ecli": "ECLI:DE:BVerwG:2023:1", "celex": "82023DE0001", "text": "z" * 800,
         "text_language": "DE", "missing_reasons": ""},
        {"ecli": "ECLI:EU:C:2024:99", "celex": "62024CJ0099", "text": "q" * 1200,
         "text_language": "FR", "missing_reasons": ""},
    ])
    return cases, fulltexts


def test_compute_coverage_stats_decade_table_split_by_text_presence():
    cases, fulltexts = _make_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)

    decade_lookup = {d: (n, wt, pct) for d, n, wt, pct in stats["decade_table"]}
    # 1960s: 1 case, 0 with text
    assert decade_lookup[1960] == (1, 0, 0.0)
    # 2000s: 1 case, 1 with text
    assert decade_lookup[2000] == (1, 1, 100.0)
    # 2020s: 4 cases (including the dup), 3 with text (the 4 unique ECLIs all
    # have text, but the duplicate row is also counted as having text via its
    # ECLI matching a fulltext row).
    n_2020, wt_2020, pct_2020 = decade_lookup[2020]
    assert n_2020 == 4 and wt_2020 == 4 and pct_2020 == 100.0


def test_compute_coverage_stats_sector_split_explodes_multi_sector_cells():
    cases, fulltexts = _make_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)

    by_sector = {sec: (n, pct) for sec, n, pct in stats["sector_table"]}
    # "6" appears 5 times: 4 rows with sector="6" + 1 row with sector="6;8"
    # "8" appears 2 times: 1 row with sector="8" + 1 row with sector="6;8"
    assert by_sector["6"][0] == 5
    assert by_sector["8"][0] == 2


def test_compute_coverage_stats_fulltext_languages_and_missing_reasons():
    cases, fulltexts = _make_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)

    langs = dict(stats["fulltext_languages"])
    assert langs == {"FR": 2, "EN": 1, "DE": 1}  # the empty-lang row dropped
    assert stats["missing_reason_top"] == [("FULLTEXT_UNAVAILABLE_UPSTREAM", 1)]
    assert stats["fulltext_total"] == 5
    assert stats["fulltext_with_text"] == 4  # the 1965 empty-text row excluded


def test_compute_coverage_stats_counts_ecli_dupes():
    cases, fulltexts = _make_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    # 6 rows, 5 unique ECLIs -> 1 duplicate
    assert stats["dup_ecli_count"] == 1


def test_compute_coverage_stats_empty_inputs_safe():
    stats = compute_coverage_stats(pd.DataFrame(), pd.DataFrame())
    assert stats["decade_table"] == []
    assert stats["sector_table"] == []
    assert stats["fulltext_total"] == 0
    assert stats["fulltext_with_text"] == 0
    assert stats["dup_ecli_count"] == 0


def test_write_dataset_card_emits_coverage_section_when_stats_provided(tmp_path):
    cases, fulltexts = _make_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)

    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=len(cases),
        fulltexts_rows=len(fulltexts),
        start_date="1965-01-01",
        end_date="2024-12-31",
        canonical_columns=["celex", "ecli"],
        discovered_columns=[],
        hf_dataset_repo="example-org/cjeu",
        coverage_stats=stats,
    )
    body = out.read_text(encoding="utf-8")

    # Section headings landed
    assert "## Coverage" in body
    assert "### Per-decade fulltext availability" in body
    assert "### Sector split" in body
    assert "### Languages" in body
    assert "### Notes" in body

    # Decade rows rendered as a markdown table
    assert "| 1960s |" in body
    assert "| 2020s |" in body

    # Sector descriptions present
    assert "EU courts" in body and "National case law" in body

    # Languages line lists at least one language
    assert "**FR**" in body

    # The schema note is in Notes section (refers to FIELDS.md)
    assert "`FIELDS.md`" in body or "FIELDS.md" in body


def test_write_dataset_card_skips_coverage_section_without_stats(tmp_path):
    """Backwards compatibility — callers that don't supply stats still work."""
    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=10,
        fulltexts_rows=10,
        start_date="2020-01-01",
        end_date="2020-01-31",
        canonical_columns=["celex"],
        discovered_columns=[],
        hf_dataset_repo="example/x",
    )
    body = out.read_text(encoding="utf-8")
    assert "## Coverage" not in body
    assert "## Headline numbers" in body  # still has the basic header


# ---------------------------------------------------------------------------
# Extended stats and richer README sections
# ---------------------------------------------------------------------------


def _make_rich_coverage_fixture():
    """Bigger fixture exercising the new stat dimensions: subjects,
    procedures, countries, citation graph topology, and most-cited cases."""
    cases = pd.DataFrame([
        {
            "ecli": "ECLI:EU:T:2003:199", "celex": "C001", "sector": "6",
            "date_publication": "2003-04-01",
            "subject_matter": "Trade marks;Intellectual, industrial and commercial property",
            "type_procedure": "Action for annulment",
            "origin_country": "Germany",
            "work_cites_work": "C002;EXTERNAL_LAW_1",
            "cited_by": "C002;C003;C004;C005;C006",
        },
        {
            "ecli": "ECLI:EU:C:2014:317", "celex": "C002", "sector": "6",
            "date_publication": "2014-05-13",
            "subject_matter": "Approximation of laws;Right to be forgotten",
            "type_procedure": "Reference for a preliminary ruling",
            "origin_country": "Spain",
            "work_cites_work": "C001",
            "cited_by": "C003;C004",
        },
        {
            "ecli": "ECLI:EU:C:2020:001", "celex": "C003", "sector": "6",
            "date_publication": "2020-01-15",
            "subject_matter": "Value added tax;Taxation",
            "type_procedure": "Reference for a preliminary ruling",
            "origin_country": "Germany",
            "work_cites_work": "C001;C002",
            "cited_by": "",
        },
        {
            "ecli": "ECLI:DE:BVerwG:2023:1", "celex": "C004", "sector": "8",
            "date_publication": "2023-06-10",
            "subject_matter": "Competition;State aids",
            "type_procedure": "Reference for a preliminary ruling",
            "origin_country": "Germany",
            "work_cites_work": "C001;EXTERNAL_TREATY_1",
            "cited_by": "",
        },
    ])
    fulltexts = pd.DataFrame([
        {"ecli": "ECLI:EU:T:2003:199", "text": "x" * 1000, "text_language": "EN",
         "missing_reasons": ""},
        {"ecli": "ECLI:EU:C:2014:317", "text": "y" * 5000, "text_language": "EN",
         "missing_reasons": ""},
        {"ecli": "ECLI:EU:C:2020:001", "text": "z" * 800, "text_language": "DE",
         "missing_reasons": ""},
        {"ecli": "ECLI:DE:BVerwG:2023:1", "text": "q" * 600, "text_language": "DE",
         "missing_reasons": ""},
    ])
    return cases, fulltexts


def test_compute_coverage_stats_top_subjects_explodes_multi_atom():
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    subjects = dict(stats["top_subjects"])
    # Each subject_matter cell has 2 atoms; "Germany" 3 cases means German
    # cases contribute different subject atoms across the rows.
    assert subjects["Trade marks"] == 1
    assert subjects["Value added tax"] == 1
    assert subjects["Competition"] == 1


def test_compute_coverage_stats_top_procedures():
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    procedures = dict(stats["top_procedures"])
    assert procedures["Reference for a preliminary ruling"] == 3
    assert procedures["Action for annulment"] == 1


def test_compute_coverage_stats_top_origin_countries():
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    countries = dict(stats["top_origin_countries"])
    assert countries["Germany"] == 3
    assert countries["Spain"] == 1


def test_compute_coverage_stats_citation_topology():
    """Internal edges (case → case in dataset) vs external (legislation etc.)"""
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    # Total edges: C001 has 2 (C002 + EXTERNAL_LAW_1)
    #              C002 has 1 (C001)
    #              C003 has 2 (C001 + C002)
    #              C004 has 2 (C001 + EXTERNAL_TREATY_1)
    # = 7 total. Internal: C002,C001,C001,C002,C001 = 5. External: 2.
    assert stats["citation_edges_total"] == 7
    assert stats["citation_edges_internal"] == 5
    assert stats["citation_edges_external"] == 2


def test_compute_coverage_stats_top_cited_cases_ordered_by_in_degree():
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    top = stats["top_cited_cases"]
    # C001 → 5 inbound, C002 → 2, C003+C004 → 0 (filtered out)
    eclis = [t[0] for t in top]
    assert eclis[0] == "ECLI:EU:T:2003:199"  # 5 cites
    assert eclis[1] == "ECLI:EU:C:2014:317"  # 2 cites
    assert len(top) == 2                     # zero-cite rows excluded
    # Each entry has (ecli, count, subject, year) and the year is parseable.
    assert top[0][1] == 5
    assert "2003" in top[0][3]


def test_write_dataset_card_emits_all_new_sections(tmp_path):
    """The rich card has Quick start, Recipes, Citation graph, demographics,
    fulltext-analysis, extraction, schema, citation (how-to-cite), and license."""
    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)

    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=len(cases),
        fulltexts_rows=len(fulltexts),
        start_date="2003-01-01", end_date="2023-12-31",
        canonical_columns=["ecli", "celex", "sector"],
        discovered_columns=[],
        hf_dataset_repo="example/cjeu",
        coverage_stats=stats,
    )
    body = out.read_text(encoding="utf-8")

    # Every major section heading present
    for heading in [
        "## Quick start",
        "## Recipes",
        "## Citation graph",
        "## What's in the corpus",
        "## Working with fulltexts",
        "## How the data was extracted",
        "## Schema",
        "## How to cite",
        "## License",
    ]:
        assert heading in body, f"missing section: {heading}"

    # Code blocks for the three loading idioms
    assert "import pandas as pd" in body
    assert "from datasets import load_dataset" in body
    assert "import polars as pl" in body

    # The recipes mention concrete query patterns
    assert "Reference for a preliminary ruling" in body  # recipe #2
    assert "PageRank" in body                            # recipe #4
    assert "fetch_text" in body                          # recipe #5

    # Demographics tables include the actual atoms from the fixture
    assert "Trade marks" in body
    assert "Germany" in body
    assert "Reference for a preliminary ruling" in body

    # Citation graph topology numbers rendered
    assert "5" in body and "7" in body  # internal + total edges

    # Most-cited table has the top case
    assert "ECLI:EU:T:2003:199" in body

    # how-to-cite block exists with a bibtex stanza
    assert "@misc" in body or "@software" in body
    assert "Apache-2.0" in body


def test_write_dataset_card_recipe_code_blocks_are_valid_python(tmp_path):
    """Any ```python``` block in the card should parse — a typo in the
    sample would otherwise ship to the dataset page."""
    import ast
    import re

    cases, fulltexts = _make_rich_coverage_fixture()
    stats = compute_coverage_stats(cases, fulltexts)
    out = tmp_path / "README.md"
    write_dataset_card(
        out,
        cases_rows=len(cases), fulltexts_rows=len(fulltexts),
        start_date="2003-01-01", end_date="2023-12-31",
        canonical_columns=["ecli"], discovered_columns=[],
        hf_dataset_repo="example/cjeu",
        coverage_stats=stats,
    )
    body = out.read_text(encoding="utf-8")
    # Match ```python ... ``` blocks (non-greedy)
    blocks = re.findall(r"```python\n(.*?)```", body, flags=re.DOTALL)
    assert blocks, "no python code blocks in card"
    for i, block in enumerate(blocks):
        try:
            ast.parse(block)
        except SyntaxError as e:
            raise AssertionError(
                f"code block #{i+1} doesn't parse: {e}\n--- block ---\n{block}"
            )
