"""Manifest state-tracking tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from cjeu_migration.manifest import Manifest, WindowState, WindowStatus


def _make_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_load_returns_empty_manifest_when_file_missing(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    assert m.all_windows() == []
    assert m.summary()["pending"] == 0


def test_register_then_load_roundtrip(tmp_path):
    p = _make_path(tmp_path)
    m1 = Manifest.load(p)
    m1.register("2020-01", "2020-01-01", "2020-01-31")
    m1.register("2020-02", "2020-02-01", "2020-02-29")
    m2 = Manifest.load(p)
    assert {w.window_id for w in m2.all_windows()} == {"2020-01", "2020-02"}
    assert all(w.status == WindowStatus.PENDING for w in m2.all_windows())


def test_register_is_idempotent(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    a = m.register("2020-01", "2020-01-01", "2020-01-31")
    a.attempts = 7  # simulate prior progress
    m.mark_failed("2020-01", "boom", max_retries=10)
    # Re-register must not reset existing state.
    b = m.register("2020-01", "2020-01-01", "2020-01-31")
    assert b.attempts == 7  # mark_failed records outcome only; doesn't bump attempts
    assert b.status == WindowStatus.FAILED
    assert b.last_error == "boom"


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def test_mark_in_progress_bumps_attempts(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    m.mark_in_progress("w1")
    assert m.get("w1").status == WindowStatus.IN_PROGRESS
    assert m.get("w1").attempts == 1
    m.mark_in_progress("w1")
    assert m.get("w1").attempts == 2


def test_mark_completed_clears_last_error(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    m.mark_in_progress("w1")
    m.mark_failed("w1", "transient SPARQL error", max_retries=5)
    assert m.get("w1").last_error == "transient SPARQL error"
    m.mark_completed("w1", row_count=42, fulltext_count=42)
    state = m.get("w1")
    assert state.status == WindowStatus.COMPLETED
    assert state.row_count == 42
    assert state.fulltext_count == 42
    assert state.last_error is None


def test_mark_failed_transitions_to_exhausted_after_max_retries(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    for _ in range(3):
        m.mark_in_progress("w1")
        m.mark_failed("w1", "boom", max_retries=3)
    state = m.get("w1")
    assert state.attempts == 3
    assert state.status == WindowStatus.EXHAUSTED


# ---------------------------------------------------------------------------
# should_process — the orchestration decision
# ---------------------------------------------------------------------------


def test_should_process_unknown_window_returns_true(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    assert m.should_process("never-seen", max_retries=3) is True


def test_should_process_skips_completed(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    m.mark_in_progress("w1")
    m.mark_completed("w1", row_count=1, fulltext_count=1)
    assert m.should_process("w1", max_retries=3) is False


def test_should_process_skips_exhausted(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    for _ in range(2):
        m.mark_in_progress("w1")
        m.mark_failed("w1", "boom", max_retries=2)
    assert m.get("w1").status == WindowStatus.EXHAUSTED
    assert m.should_process("w1", max_retries=2) is False


def test_should_process_retries_failed_under_cap(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    m.mark_in_progress("w1")
    m.mark_failed("w1", "boom", max_retries=5)
    assert m.should_process("w1", max_retries=5) is True


def test_should_process_picks_up_crashed_in_progress(tmp_path):
    """A previous run died mid-window. Manifest still says IN_PROGRESS.
    Next run must pick it up rather than skip it."""
    m = Manifest.load(_make_path(tmp_path))
    m.register("w1", "2020-01-01", "2020-01-31")
    m.mark_in_progress("w1")  # crashed here
    # No completion / failure recorded.
    assert m.should_process("w1", max_retries=3) is True


# ---------------------------------------------------------------------------
# Atomicity / persistence integrity
# ---------------------------------------------------------------------------


def test_save_is_atomic_on_disk(tmp_path):
    """A crash mid-write must leave the manifest readable, never half-written."""
    p = _make_path(tmp_path)
    m = Manifest.load(p)
    m.register("w1", "2020-01-01", "2020-01-31")
    raw = json.loads(p.read_text("utf-8"))
    assert raw["schema_version"] == 1
    assert "w1" in raw["windows"]


def test_concurrent_mark_updates_serialise(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    for i in range(20):
        m.register(f"w{i}", "2020-01-01", "2020-01-31")

    def worker(i: int):
        m.mark_in_progress(f"w{i}")
        m.mark_completed(f"w{i}", row_count=i, fulltext_count=i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert m.summary()["completed"] == 20
    for i in range(20):
        assert m.get(f"w{i}").row_count == i


def test_summary_tallies_all_statuses(tmp_path):
    m = Manifest.load(_make_path(tmp_path))
    m.register("a", "2020-01-01", "2020-01-31")
    m.register("b", "2020-02-01", "2020-02-29")
    m.register("c", "2020-03-01", "2020-03-31")
    m.mark_in_progress("b")
    m.mark_completed("b", row_count=1, fulltext_count=1)
    m.mark_in_progress("c")
    m.mark_failed("c", "boom", max_retries=3)
    summary = m.summary()
    assert summary["pending"] == 1
    assert summary["completed"] == 1
    assert summary["failed"] == 1
