"""Tests for the SQL-runner CJEU text-sync preflight."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("SQL_RUNNER_URL", "https://runner.invalid")
os.environ.setdefault("SQL_RUNNER_TOKEN", "test-token")
_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "migration"
    / "sql"
    / "60_sync_cjeu_texts_via_runner.py"
)
_spec = importlib.util.spec_from_file_location("sync_cjeu_texts_via_runner", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _row(*, celex="62020CJ0414", text="JUDGMENT OF THE COURT Full text"):
    return {
        "ecli": "ECLI:EU:C:2021:1",
        "celex": celex,
        "text": text,
        "text_source": "CELLAR_ITEM",
        "text_language": "EN",
        "text_format": "xhtml",
        "missing_reasons": "",
    }


def test_select_target_rows_accepts_base_judgment_manifestation():
    targets = {"ECLI:EU:C:2021:1": "62020CJ0414"}
    assert mod.select_target_rows([_row()], targets) == [_row()]


def test_select_target_rows_accepts_joined_english_badge():
    targets = {"ECLI:EU:C:2021:1": "62020CJ0414"}
    row = _row(text="Provisional text ENJUDGMENT OF THE COURT Full text")
    assert mod.select_target_rows([row], targets) == [row]


@pytest.mark.parametrize("suffix", ["SUM", "RES", "INF"])
def test_select_target_rows_rejects_nonjudgment_manifestation(suffix):
    targets = {"ECLI:EU:C:2021:1": "62020CJ0414"}
    with pytest.raises(SystemExit, match="target preflight failed"):
        mod.select_target_rows([_row(celex=f"62020CJ0414_{suffix}")], targets)


def test_select_target_rows_rejects_nonjudgment_text():
    targets = {"ECLI:EU:C:2021:1": "62020CJ0414"}
    with pytest.raises(SystemExit, match="not a full judgment"):
        mod.select_target_rows([_row(text="Opinion of the Advocate General")], targets)


def test_same_source_content_hash_detects_replacement():
    old_hash = mod.content_md5("old CELLAR summary")

    assert mod.content_needs_update(old_hash, "full CELLAR judgment")
    assert not mod.content_needs_update(old_hash, "old CELLAR summary")
    assert mod.content_needs_update(old_hash, "old CELLAR summary", force=True)
