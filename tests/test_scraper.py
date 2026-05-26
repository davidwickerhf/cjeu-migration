"""Scraper retry / output-shape tests using a stub for cellar-extractor."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pytest

from cjeu_migration.scraper import ScrapeError, scrape_window
from cjeu_migration.windowing import Window


def _stub_writer(
    *, cases_rows: list, fulltext_entries: list
) -> Callable[..., Any]:
    """Build a fake cellar-extractor that writes the given fixture data."""

    def _fake_extra(**kwargs):
        cases_path = Path(kwargs["metadata_output_path"])
        fulltext_path = Path(kwargs["fulltext_output_path"])
        cases_path.parent.mkdir(parents=True, exist_ok=True)
        fulltext_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a header + N data rows.
        with cases_path.open("w", encoding="utf-8") as f:
            f.write("celex,ecli,sector\n")
            for row in cases_rows:
                f.write(",".join(row) + "\n")
        with fulltext_path.open("w", encoding="utf-8") as f:
            json.dump(fulltext_entries, f)

    return _fake_extra


def _window() -> Window:
    return Window(window_id="2020-01", sd=date(2020, 1, 1), ed=date(2020, 1, 31))


def test_scrape_window_writes_outputs_and_counts(tmp_path):
    cases_dir = tmp_path / "cases"
    fulltexts_dir = tmp_path / "fulltexts"
    stub = _stub_writer(
        cases_rows=[
            ("62020CJ0001", "ECLI:EU:C:2020:1", "6"),
            ("62020CJ0002", "ECLI:EU:C:2020:2", "6"),
        ],
        fulltext_entries=[
            {"celex": "62020CJ0001", "ecli": "ECLI:EU:C:2020:1", "text": "a"},
            {"celex": "62020CJ0002", "ecli": "ECLI:EU:C:2020:2", "text": "b"},
        ],
    )

    result = scrape_window(
        _window(),
        cases_dir=cases_dir,
        fulltexts_dir=fulltexts_dir,
        extra_fn=stub,
        max_attempts=3,
    )

    assert result.row_count == 2
    assert result.fulltext_count == 2
    assert result.cases_path.exists()
    assert result.fulltexts_path.exists()
    assert result.cases_path.name == "2020-01.csv"
    assert result.fulltexts_path.name == "2020-01.json"
    assert result.attempts == 1


def test_scrape_window_retries_then_succeeds(tmp_path):
    """First two calls raise, third succeeds. The wrapper must retry transparently."""
    cases_dir = tmp_path / "cases"
    fulltexts_dir = tmp_path / "fulltexts"
    calls = {"n": 0}
    inner_stub = _stub_writer(
        cases_rows=[("62020CJ0001", "ECLI:EU:C:2020:1", "6")],
        fulltext_entries=[{"celex": "62020CJ0001"}],
    )

    def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient SPARQL outage")
        inner_stub(**kwargs)

    result = scrape_window(
        _window(),
        cases_dir=cases_dir,
        fulltexts_dir=fulltexts_dir,
        extra_fn=_flaky,
        max_attempts=5,
    )
    assert calls["n"] == 3
    assert result.attempts == 3
    assert result.row_count == 1


def test_scrape_window_raises_scrape_error_after_exhausted_retries(tmp_path):
    cases_dir = tmp_path / "cases"
    fulltexts_dir = tmp_path / "fulltexts"

    def _always_fails(**kwargs):
        raise RuntimeError("endpoint down")

    with pytest.raises(ScrapeError) as exc_info:
        scrape_window(
            _window(),
            cases_dir=cases_dir,
            fulltexts_dir=fulltexts_dir,
            extra_fn=_always_fails,
            max_attempts=2,
        )
    assert "2020-01" in str(exc_info.value)
    assert "2 attempts" in str(exc_info.value)


def test_scrape_window_handles_empty_window(tmp_path):
    """A window with no documents must still write a header-only CSV and an
    empty fulltext list, returning row_count=0."""
    cases_dir = tmp_path / "cases"
    fulltexts_dir = tmp_path / "fulltexts"
    stub = _stub_writer(cases_rows=[], fulltext_entries=[])
    result = scrape_window(
        _window(),
        cases_dir=cases_dir,
        fulltexts_dir=fulltexts_dir,
        extra_fn=stub,
        max_attempts=3,
    )
    assert result.row_count == 0
    assert result.fulltext_count == 0


def test_scrape_window_passes_sd_ed_threads_max_ecli_to_extractor(tmp_path):
    """Wire-through smoke test — runner-supplied params must reach cellar-extractor."""
    cases_dir = tmp_path / "cases"
    fulltexts_dir = tmp_path / "fulltexts"
    seen_kwargs = {}

    def _capture(**kwargs):
        seen_kwargs.update(kwargs)
        # Still need to write the files so post-processing succeeds.
        Path(kwargs["metadata_output_path"]).write_text("celex,ecli\nA,B\n")
        Path(kwargs["fulltext_output_path"]).write_text("[]")

    scrape_window(
        _window(),
        cases_dir=cases_dir,
        fulltexts_dir=fulltexts_dir,
        extra_fn=_capture,
        max_attempts=1,
        threads=7,
        max_ecli=42,
    )
    assert seen_kwargs["sd"] == "2020-01-01"
    assert seen_kwargs["ed"] == "2020-01-31T23:59:59"
    assert seen_kwargs["threads"] == 7
    assert seen_kwargs["max_ecli"] == 42
    assert seen_kwargs["save"] is True
    assert seen_kwargs["return_data"] is False
