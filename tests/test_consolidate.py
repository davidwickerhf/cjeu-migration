"""Tests for the window-to-parquet consolidation step."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cjeu_migration.consolidate import (
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
