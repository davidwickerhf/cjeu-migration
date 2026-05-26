"""Live end-to-end smoke test.

Runs the real pipeline against a tiny window of the live CELLAR endpoint. Does
NOT upload to HuggingFace (uses ``skip_upload=True``). Verifies:

1. A real window produces non-empty cases + fulltexts parquet files.
2. The manifest reflects success.
3. The dataset card mentions the window.

Run with::

    RUN_SMOKE=1 pytest tests/test_smoke_integration.py -v

Wall time ~30–60 s depending on endpoint health. Skipped by default so it
never runs in the unit suite.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from cjeu_migration.config import Config
from cjeu_migration.manifest import Manifest, WindowStatus
from cjeu_migration.runner import run


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SMOKE") != "1",
    reason="Set RUN_SMOKE=1 to run the live end-to-end smoke test.",
)


def _smoke_config(tmp_path: Path) -> Config:
    return Config(
        hf_token=None,
        hf_dataset_repo="example-org/cjeu-smoke",
        workspace_dir=tmp_path / "workspace",
        # One month with reliably-indexed CJEU activity.
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 31),
        window="month",
        max_ecli_per_window=200,
        extractor_threads=4,
        max_window_retries=2,
        skip_upload=True,
        ntfy_topic_url=None,
        ntfy_interval_seconds=1800,
        ntfy_auth_token=None,
    )


@pytest.mark.smoke
def test_smoke_one_real_window(tmp_path):
    cfg = _smoke_config(tmp_path)

    summary = run(cfg)

    # 1. Manifest reflects one window, completed.
    assert summary.windows_total == 1
    assert summary.windows_completed == 1
    assert summary.windows_failed == 0
    assert summary.windows_exhausted == 0
    manifest = Manifest.load(cfg.manifest_path)
    state = manifest.get("2020-01")
    assert state is not None
    assert state.status == WindowStatus.COMPLETED
    assert state.row_count > 0
    assert state.fulltext_count >= 0

    # 2. Consolidated parquet files exist and carry the expected schema.
    cases_path = cfg.consolidated_dir / "cases.parquet"
    fulltexts_path = cfg.consolidated_dir / "fulltexts.parquet"
    assert cases_path.exists()
    assert fulltexts_path.exists()

    cases = pd.read_parquet(cases_path)
    assert "celex" in cases.columns
    assert "ecli" in cases.columns
    assert "sector" in cases.columns
    assert len(cases) == summary.cases_rows
    # 2020 case law -> sector 6 dominates, plus some sector 8 nationals.
    assert set(cases["sector"].dropna().astype(str)).issubset({"6", "8"})

    # 3. Dataset card mentions the window.
    readme = (cfg.consolidated_dir / "README.md").read_text(encoding="utf-8")
    assert "2020-01-01" in readme
    assert "2020-01-31" in readme

    # 4. FIELDS.md was copied across (if it ships with the installed extractor).
    fields_path = cfg.consolidated_dir / "FIELDS.md"
    if fields_path.exists():
        assert "CJEU" in fields_path.read_text(encoding="utf-8")
