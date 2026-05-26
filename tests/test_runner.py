"""Runner orchestration tests — uses fakes for both scraping and HF upload."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from cjeu_migration.config import Config
from cjeu_migration.manifest import Manifest, WindowStatus
from cjeu_migration.runner import run
from cjeu_migration.scraper import ScrapeError, ScrapeResult
from cjeu_migration.windowing import Window


def _build_config(tmp_path: Path, *, hf_token: str = "fake-token", skip_upload: bool = False) -> Config:
    return Config(
        hf_token=hf_token,
        hf_dataset_repo="example-org/cjeu-cases",
        workspace_dir=tmp_path / "workspace",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 3, 31),  # 3 months -> 3 windows
        window="month",
        max_ecli_per_window=100,
        extractor_threads=2,
        max_window_retries=2,
        skip_upload=skip_upload,
    )


def _write_window_files(
    cfg: Config,
    window: Window,
    *,
    cases_rows: list,
    fulltext_rows: list,
) -> ScrapeResult:
    cases_path = cfg.cases_dir / f"{window.window_id}.csv"
    fulltext_path = cfg.fulltexts_dir / f"{window.window_id}.json"
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    fulltext_path.parent.mkdir(parents=True, exist_ok=True)
    with cases_path.open("w", encoding="utf-8") as f:
        f.write("celex,ecli,sector\n")
        for row in cases_rows:
            f.write(",".join(row) + "\n")
    fulltext_path.write_text(json.dumps(fulltext_rows), encoding="utf-8")
    return ScrapeResult(
        window=window,
        row_count=len(cases_rows),
        fulltext_count=len(fulltext_rows),
        cases_path=cases_path,
        fulltexts_path=fulltext_path,
        attempts=1,
    )


def _ok_scrape_fn(*, window_outputs: dict):
    """Return a fake scrape_fn that writes the supplied per-window fixtures."""

    def _fake(window, *, cases_dir, fulltexts_dir, threads, max_ecli, max_attempts):
        # Build a temporary cfg so _write_window_files can derive paths.
        cfg_like = type("X", (), {"cases_dir": cases_dir, "fulltexts_dir": fulltexts_dir})
        cases_rows, fulltext_rows = window_outputs[window.window_id]
        return _write_window_files(cfg_like, window, cases_rows=cases_rows, fulltext_rows=fulltext_rows)

    return _fake


def _capture_push_fn(calls: list):
    def _fake_push(dataset_dir, repo_id, *, token, commit_message=None):
        calls.append(
            {
                "dataset_dir": str(dataset_dir),
                "repo_id": repo_id,
                "token": token,
                "commit_message": commit_message,
            }
        )
        return ["README.md", "FIELDS.md", "cases.parquet", "fulltexts.parquet"]

    return _fake_push


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_full_pipeline_happy_path(tmp_path):
    cfg = _build_config(tmp_path)
    outputs = {
        "2020-01": (
            [("62020CJ0001", "ECLI:EU:C:2020:1", "6")],
            [{"celex": "62020CJ0001", "ecli": "ECLI:EU:C:2020:1", "text": "a"}],
        ),
        "2020-02": (
            [("62020CJ0002", "ECLI:EU:C:2020:2", "6")],
            [{"celex": "62020CJ0002", "ecli": "ECLI:EU:C:2020:2", "text": "b"}],
        ),
        "2020-03": (
            [
                ("62020CJ0003", "ECLI:EU:C:2020:3", "6"),
                ("62020CJ0004", "ECLI:EU:C:2020:4", "6"),
            ],
            [
                {"celex": "62020CJ0003", "ecli": "ECLI:EU:C:2020:3", "text": "c"},
                {"celex": "62020CJ0004", "ecli": "ECLI:EU:C:2020:4", "text": "d"},
            ],
        ),
    }
    push_calls: list = []

    summary = run(
        cfg,
        scrape_fn=_ok_scrape_fn(window_outputs=outputs),
        push_fn=_capture_push_fn(push_calls),
    )

    assert summary.windows_total == 3
    assert summary.windows_completed == 3
    assert summary.windows_failed == 0
    assert summary.windows_exhausted == 0
    assert summary.cases_rows == 4
    assert summary.fulltexts_rows == 4
    assert summary.uploaded == ["README.md", "FIELDS.md", "cases.parquet", "fulltexts.parquet"]
    # Push was called with the consolidated directory and the right repo.
    assert len(push_calls) == 1
    assert push_calls[0]["repo_id"] == "example-org/cjeu-cases"
    assert push_calls[0]["token"] == "fake-token"
    # Output parquets exist.
    assert (cfg.consolidated_dir / "cases.parquet").exists()
    assert (cfg.consolidated_dir / "fulltexts.parquet").exists()
    assert (cfg.consolidated_dir / "README.md").exists()


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_run_continues_after_single_window_failure(tmp_path):
    """One window blowing up must not stop the rest from running."""
    cfg = _build_config(tmp_path)
    outputs = {
        "2020-01": ([("62020CJ0001", "ECLI:EU:C:2020:1", "6")], [{"celex": "62020CJ0001"}]),
        "2020-03": ([("62020CJ0003", "ECLI:EU:C:2020:3", "6")], [{"celex": "62020CJ0003"}]),
    }
    ok_fn = _ok_scrape_fn(window_outputs=outputs)

    def _selective_fn(window, **kwargs):
        if window.window_id == "2020-02":
            raise ScrapeError("simulated SPARQL outage on 2020-02")
        return ok_fn(window, **kwargs)

    push_calls: list = []
    summary = run(cfg, scrape_fn=_selective_fn, push_fn=_capture_push_fn(push_calls))

    assert summary.windows_total == 3
    # 2020-02 should be exhausted after one attempt-then-fail at max_retries=2.
    # The runner marks failed each time without bumping attempts (mark_in_progress does).
    # So 2020-02 hits FAILED status (attempts=1 < max_retries=2) -> retries not triggered in one run.
    assert summary.windows_completed == 2
    # 2020-02 is either FAILED or EXHAUSTED depending on retry budget; either way not completed.
    assert (summary.windows_failed + summary.windows_exhausted) == 1
    assert summary.cases_rows == 2
    assert summary.fulltexts_rows == 2
    # Push still happens with what we have.
    assert len(push_calls) == 1


# ---------------------------------------------------------------------------
# Resumability
# ---------------------------------------------------------------------------


def test_run_skips_completed_windows_on_restart(tmp_path):
    cfg = _build_config(tmp_path)
    outputs = {
        "2020-01": ([("62020CJ0001", "ECLI:EU:C:2020:1", "6")], [{"celex": "62020CJ0001"}]),
        "2020-02": ([("62020CJ0002", "ECLI:EU:C:2020:2", "6")], [{"celex": "62020CJ0002"}]),
        "2020-03": ([("62020CJ0003", "ECLI:EU:C:2020:3", "6")], [{"celex": "62020CJ0003"}]),
    }
    # First run completes everything.
    run(
        cfg,
        scrape_fn=_ok_scrape_fn(window_outputs=outputs),
        push_fn=_capture_push_fn([]),
    )
    # Now scrape_fn must NOT be called again on a second run.
    second_calls: list = []

    def _record_call(window, **_kwargs):
        second_calls.append(window.window_id)
        raise AssertionError("scrape_fn called for completed window: " + window.window_id)

    summary = run(cfg, scrape_fn=_record_call, push_fn=_capture_push_fn([]))
    assert second_calls == []
    # Manifest still reports all completed.
    assert summary.windows_completed == 3


def test_run_retries_failed_windows_on_restart_within_budget(tmp_path):
    cfg = _build_config(tmp_path)
    cfg = replace(cfg, max_window_retries=3)

    # First run: 2020-02 fails.
    def _fails_once(window, **_kwargs):
        if window.window_id == "2020-02":
            raise ScrapeError("first attempt failed")
        return _write_window_files(
            type("X", (), {"cases_dir": _kwargs["cases_dir"], "fulltexts_dir": _kwargs["fulltexts_dir"]}),
            window,
            cases_rows=[(f"X-{window.window_id}", "Y", "6")],
            fulltext_rows=[{"celex": f"X-{window.window_id}"}],
        )

    run(cfg, scrape_fn=_fails_once, push_fn=_capture_push_fn([]))
    manifest = Manifest.load(cfg.manifest_path)
    assert manifest.get("2020-02").status == WindowStatus.FAILED

    # Second run: same window succeeds.
    def _succeeds_second(window, **kwargs):
        return _write_window_files(
            type("X", (), {"cases_dir": kwargs["cases_dir"], "fulltexts_dir": kwargs["fulltexts_dir"]}),
            window,
            cases_rows=[(f"X-{window.window_id}", "Y", "6")],
            fulltext_rows=[{"celex": f"X-{window.window_id}"}],
        )

    summary = run(cfg, scrape_fn=_succeeds_second, push_fn=_capture_push_fn([]))
    assert summary.windows_completed == 3
    manifest = Manifest.load(cfg.manifest_path)
    assert manifest.get("2020-02").status == WindowStatus.COMPLETED


# ---------------------------------------------------------------------------
# Upload skipping
# ---------------------------------------------------------------------------


def test_run_skips_push_when_skip_upload_flag_set(tmp_path):
    cfg = _build_config(tmp_path, skip_upload=True)
    outputs = {
        "2020-01": ([("A", "B", "6")], [{"celex": "A"}]),
        "2020-02": ([("C", "D", "6")], [{"celex": "C"}]),
        "2020-03": ([("E", "F", "6")], [{"celex": "E"}]),
    }
    push_calls: list = []
    summary = run(
        cfg,
        scrape_fn=_ok_scrape_fn(window_outputs=outputs),
        push_fn=_capture_push_fn(push_calls),
    )
    assert push_calls == []
    assert summary.uploaded == []
    # But the consolidated outputs are still on disk.
    assert (cfg.consolidated_dir / "cases.parquet").exists()


def test_run_skips_push_when_no_hf_token(tmp_path):
    cfg = _build_config(tmp_path, hf_token=None)
    outputs = {f"2020-0{i}": ([("X", "Y", "6")], [{"celex": "X"}]) for i in range(1, 4)}
    push_calls: list = []
    summary = run(
        cfg,
        scrape_fn=_ok_scrape_fn(window_outputs=outputs),
        push_fn=_capture_push_fn(push_calls),
    )
    assert push_calls == []
    assert summary.uploaded == []


# ---------------------------------------------------------------------------
# consolidate-only path
# ---------------------------------------------------------------------------


def test_run_consolidate_only_skips_scrape(tmp_path):
    cfg = _build_config(tmp_path)
    # Pre-populate workspace with window outputs.
    outputs = {
        "2020-01": ([("62020CJ0001", "ECLI:EU:C:2020:1", "6")], [{"celex": "62020CJ0001"}]),
        "2020-02": ([("62020CJ0002", "ECLI:EU:C:2020:2", "6")], [{"celex": "62020CJ0002"}]),
        "2020-03": ([("62020CJ0003", "ECLI:EU:C:2020:3", "6")], [{"celex": "62020CJ0003"}]),
    }
    for window_id, (rows, fulltext) in outputs.items():
        cases_path = cfg.cases_dir / f"{window_id}.csv"
        fulltext_path = cfg.fulltexts_dir / f"{window_id}.json"
        cases_path.parent.mkdir(parents=True, exist_ok=True)
        fulltext_path.parent.mkdir(parents=True, exist_ok=True)
        with cases_path.open("w", encoding="utf-8") as f:
            f.write("celex,ecli,sector\n")
            for r in rows:
                f.write(",".join(r) + "\n")
        fulltext_path.write_text(json.dumps(fulltext))

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("scrape_fn must not run in consolidate_only mode")

    push_calls: list = []
    summary = run(
        cfg,
        scrape_fn=_must_not_be_called,
        push_fn=_capture_push_fn(push_calls),
        consolidate_only=True,
    )
    assert summary.cases_rows == 3
    assert summary.fulltexts_rows == 3
    assert len(push_calls) == 1
