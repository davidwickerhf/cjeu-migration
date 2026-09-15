"""Tests for Kamil's targeted English-gap verifier."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "migration"
    / "verify"
    / "verify_kamil_en_gaps.py"
)
_spec = importlib.util.spec_from_file_location("verify_kamil_en_gaps", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def test_scan_and_classify_targeted_english_rows(tmp_path):
    parquet_path = tmp_path / "fulltexts.parquet"
    pq.write_table(
        pa.table(
            {
                "ecli": [
                    "ECLI:EU:C:2024:1",
                    "ECLI:EU:C:2024:2",
                    "ECLI:EU:C:2024:3",
                    "ECLI:EU:C:2024:4",
                    "ECLI:EU:C:2024:999",
                ],
                "celex": [
                    "62024CJ0001",
                    "62024CJ0002",
                    "62024CJ0003",
                    "62024CJ9999",
                    "62024CJ0999",
                ],
                "text": [
                    "JUDGMENT OF THE COURT The Court hereby rules:",
                    "Opinion of the Advocate General in this case",
                    "Judgment of the Court on the merits",
                    "Judgment of the Court on the merits",
                    "Judgment of the Court outside the target set",
                ],
                "text_source": ["CELLAR_ITEM"] * 5,
                "text_language": ["en", "EN", "ENG", "ENGLISH", "EN"],
                "text_format": ["HTML"] * 5,
            }
        ),
        parquet_path,
    )
    expected = [
        {"case": "C-1/24", "ecli": "ECLI:EU:C:2024:1", "celex": "62024CJ0001"},
        {"case": "C-2/24", "ecli": "ECLI:EU:C:2024:2", "celex": "62024CJ0002"},
        {"case": "C-3/24", "ecli": "ECLI:EU:C:2024:3", "celex": "62024CJ0003"},
        {"case": "C-4/24", "ecli": "ECLI:EU:C:2024:4", "celex": "62024CJ0004"},
        {"case": "C-5/24", "ecli": "ECLI:EU:C:2024:5", "celex": "62024CJ0005"},
    ]

    found = mod.scan_english_rows(parquet_path, {row["ecli"] for row in expected})
    results = mod.classify(expected, found)

    assert [row["status"] for row in results] == [
        "pass",
        "suspect_not_judgment",
        "pass",
        "non_judgment_manifestation",
        "missing_english",
    ]
    assert "ECLI:EU:C:2024:999" not in found
