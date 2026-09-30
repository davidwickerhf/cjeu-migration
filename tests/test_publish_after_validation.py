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
        "duplicate_celex_groups": 0,
        "malformed_celex": 0,
        "duplicate_ecli_language_pairs": 0,
        "derived_fulltext_bodies": 0,
        "baseline": {"lost_eclis": 0, "lost_cellar_eclis": 0},
    }


def good_identity() -> dict:
    return {
        "required_identities": len(mod.REQUIRED_CATALOG_IDENTITIES),
        "missing_required": [],
        "mismatched_required": {},
        "identity_conflicts": {},
        "stale_eclis_present": [],
    }


def test_validate_gates_accepts_exact_expected_results():
    mod.validate_gates(good_live(), good_integrity(), good_identity())


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("live", "residuals", 1),
        ("live", "passed", 603),
        ("integrity", "duplicate_case_eclis", 1),
        ("integrity", "derived_fulltext_bodies", 1),
        ("integrity", "duplicate_celex_groups", 3),
        ("integrity", "malformed_celex", 1),
        ("integrity", "baseline", {"lost_eclis": 0, "lost_cellar_eclis": 290}),
        ("integrity", "baseline", None),
        ("integrity", "fulltexts_rows", mod.MIN_FULLTEXT_ROWS - 1),
        ("identity", "stale_eclis_present", ["ECLI:EU:C:2012:820"]),
    ],
)
def test_validate_gates_rejects_any_failed_gate(section, key, value):
    live = good_live()
    integrity = good_integrity()
    identity = good_identity()
    target = (
        live if section == "live" else integrity if section == "integrity" else identity
    )
    target[key] = value

    with pytest.raises(RuntimeError):
        mod.validate_gates(live, integrity, identity)


def test_validate_catalog_identities_streams_required_mappings(tmp_path):
    import pandas as pd

    rows = [
        {"ecli": ecli, "celex": celex}
        for ecli, celex in mod.REQUIRED_CATALOG_IDENTITIES.items()
    ]
    path = tmp_path / "cases.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)

    summary = mod.validate_catalog_identities(path)

    assert summary["required_identities"] == len(mod.REQUIRED_CATALOG_IDENTITIES)
    assert summary["missing_required"] == []


def test_validate_catalog_identities_rejects_stale_alias(tmp_path):
    import pandas as pd

    rows = [
        {"ecli": ecli, "celex": celex}
        for ecli, celex in mod.REQUIRED_CATALOG_IDENTITIES.items()
    ]
    rows.append({"ecli": "ECLI:EU:C:2012:820", "celex": "62011CJ0279"})
    path = tmp_path / "cases.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)

    with pytest.raises(RuntimeError, match="stale_eclis_present"):
        mod.validate_catalog_identities(path)
