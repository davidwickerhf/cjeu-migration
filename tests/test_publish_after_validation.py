"""Tests for the unattended validation-to-publish gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "vastai"
    / "publish_after_validation.py"
)
_spec = importlib.util.spec_from_file_location("publish_after_validation", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def good_live() -> dict:
    return {
        "expected_cases": 604,
        "passed": 604,
        "residuals": 0,
        "live_status_counts": {"exact_match": 604},
    }


def good_integrity() -> dict:
    return {
        "cases_rows": 46_638,
        "fulltexts_rows": 608_668,
        "duplicate_case_eclis": 0,
        "duplicate_ecli_language_pairs": 0,
        "derived_fulltext_bodies": 0,
    }


def test_validate_gates_accepts_exact_expected_results():
    mod.validate_gates(good_live(), good_integrity())


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("live", "residuals", 1),
        ("live", "passed", 603),
        ("integrity", "duplicate_case_eclis", 1),
        ("integrity", "derived_fulltext_bodies", 1),
        ("integrity", "fulltexts_rows", 608_667),
    ],
)
def test_validate_gates_rejects_any_failed_gate(section, key, value):
    live = good_live()
    integrity = good_integrity()
    (live if section == "live" else integrity)[key] = value

    with pytest.raises(RuntimeError):
        mod.validate_gates(live, integrity)
