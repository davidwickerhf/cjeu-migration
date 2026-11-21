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


def test_looks_like_judgment_accepts_joined_english_badge():
    assert mod.looks_like_judgment(
        "Provisional text ENJUDGMENT OF THE COURT (First Chamber)"
    )


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


def test_live_cellar_comparison_requires_full_body_match(tmp_path):
    parquet_path = tmp_path / "fulltexts.parquet"
    stored_exact = "Judgment of the Court\nThe Court hereby rules: appeal dismissed."
    stored_mismatch = "Judgment of the Court The Court hereby rules: short text."
    pq.write_table(
        pa.table(
            {
                "ecli": ["ECLI:EU:C:2024:1", "ECLI:EU:C:2024:2"],
                "celex": ["62024CJ0001", "62024CJ0002"],
                "text": [stored_exact, stored_mismatch],
                "text_source": ["CELLAR_ITEM", "CELLAR_ITEM"],
                "text_language": ["EN", "EN"],
            }
        ),
        parquet_path,
    )
    expected = [
        {"case": "C-1/24", "ecli": "ECLI:EU:C:2024:1", "celex": "62024CJ0001"},
        {"case": "C-2/24", "ecli": "ECLI:EU:C:2024:2", "celex": "62024CJ0002"},
    ]
    live = {
        "62024CJ0001": " Judgment   of the Court The Court hereby rules: appeal dismissed. ",
        "62024CJ0002": (
            "Judgment of the Court The Court hereby rules: complete judgment body."
        ),
    }

    found = mod.scan_english_rows(parquet_path, {row["ecli"] for row in expected})
    results = mod.classify(expected, found)
    mod.compare_live_cellar(
        expected,
        found,
        results,
        max_workers=2,
        fetch_fn=lambda celex: live[celex],
    )

    assert [row["live_status"] for row in results] == [
        "exact_match",
        "content_mismatch",
    ]
    assert results[0]["stored_sha256"] == results[0]["live_sha256"]
    assert results[1]["stored_sha256"] != results[1]["live_sha256"]
