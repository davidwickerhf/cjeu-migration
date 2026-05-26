"""ProgressNotifier tests — uses a fake HTTP post and a fake clock."""

from __future__ import annotations

import threading
import time
from typing import List

import pytest

from cjeu_migration.notifier import NtfyConfig, ProgressNotifier


class _FakePoster:
    """Captures every HTTP POST the notifier issues. Returns 200 by default."""

    def __init__(self, status: int = 200):
        self.calls: List[dict] = []
        self.status = status

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float) -> int:
        self.calls.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        return self.status


def _config(interval: int = 60, token: str = None) -> NtfyConfig:
    return NtfyConfig(topic_url="https://ntfy.sh/test-topic", interval_seconds=interval, auth_token=token)


# ---------------------------------------------------------------------------
# Explicit event notifications
# ---------------------------------------------------------------------------


def test_notify_run_started_posts_with_rocket_tag():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_run_started(start_date="2020-01-01", end_date="2020-01-31", total_windows=1)
    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["url"] == "https://ntfy.sh/test-topic"
    assert call["headers"]["Title"].startswith("CJEU migration started")
    assert call["headers"]["Tags"] == "rocket"
    assert b"2020-01-01" in call["body"]
    assert b"Total windows: 1" in call["body"]


def test_notify_run_finished_clean_completion():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_run_finished(
        summary={"completed": 12, "failed": 0, "exhausted": 0, "pending": 0, "in_progress": 0},
        cases_rows=1234,
        fulltexts_rows=1230,
    )
    headers = poster.calls[0]["headers"]
    body = poster.calls[0]["body"]
    assert "done" in headers["Title"].lower()
    assert headers["Priority"] == "high"
    assert headers["Tags"] == "white_check_mark"
    assert b"1,234" in body
    assert b"1,230" in body


def test_notify_run_finished_with_exhausted_marks_warning():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_run_finished(
        summary={"completed": 10, "failed": 0, "exhausted": 2, "pending": 0, "in_progress": 0},
        cases_rows=100, fulltexts_rows=100,
    )
    headers = poster.calls[0]["headers"]
    assert "exhausted" in headers["Title"].lower()
    assert headers["Tags"] == "warning"


def test_notify_fatal_error_uses_urgent_priority():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_fatal_error("OperationalError('boom')")
    call = poster.calls[0]
    assert call["headers"]["Priority"] == "urgent"
    assert call["headers"]["Tags"] == "rotating_light"
    assert b"boom" in call["body"]


def test_notify_fatal_error_truncates_long_messages():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_fatal_error("X" * 5000)
    assert len(poster.calls[0]["body"]) <= 1500


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------


def test_auth_token_attaches_bearer_header():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(token="secret-bearer"), lambda: {}, http_post=poster)
    notifier.notify_run_started(start_date="2020-01-01", end_date="2020-01-31", total_windows=1)
    assert poster.calls[0]["headers"]["Authorization"] == "Bearer secret-bearer"


def test_no_auth_token_no_bearer_header():
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_run_started(start_date="2020-01-01", end_date="2020-01-31", total_windows=1)
    assert "Authorization" not in poster.calls[0]["headers"]


# ---------------------------------------------------------------------------
# Periodic progress posting
# ---------------------------------------------------------------------------


def test_progress_post_includes_completed_total_eta():
    """2 windows done in 100s → 50s/window. 8 remaining → 400s = 6m40s."""
    poster = _FakePoster()
    notifier = ProgressNotifier(
        _config(),
        manifest_summary_fn=lambda: {
            "completed": 2, "pending": 8, "in_progress": 0, "failed": 0, "exhausted": 0
        },
        http_post=poster,
        clock=lambda: 100.0,  # _clock() always returns 100, _started_at = 0 → 100s elapsed
    )
    notifier._started_at = 0.0
    notifier._post_progress()
    call = poster.calls[0]
    assert call["headers"]["Title"] == "CJEU migration: 2/10 windows"
    body = call["body"].decode("utf-8")
    assert "Completed: 2/10" in body
    assert "Elapsed: 1m40s" in body
    assert "ETA: 6m40s" in body


def test_progress_post_dash_eta_before_any_completion():
    poster = _FakePoster()
    notifier = ProgressNotifier(
        _config(),
        manifest_summary_fn=lambda: {
            "completed": 0, "pending": 10, "in_progress": 0, "failed": 0, "exhausted": 0
        },
        http_post=poster,
        clock=lambda: 0.0,
    )
    notifier._started_at = 0.0
    notifier._post_progress()
    body = poster.calls[0]["body"].decode("utf-8")
    assert "ETA: —" in body


# ---------------------------------------------------------------------------
# Background-thread lifecycle
# ---------------------------------------------------------------------------


def test_start_then_stop_ticks_at_interval(monkeypatch):
    poster = _FakePoster()
    tick_count = {"n": 0}

    def _summary():
        tick_count["n"] += 1
        return {"completed": tick_count["n"], "pending": 0, "in_progress": 0,
                "failed": 0, "exhausted": 0}

    # 100ms tick interval so the test stays fast.
    notifier = ProgressNotifier(
        NtfyConfig(topic_url="https://ntfy.sh/t", interval_seconds=0.1, auth_token=None),
        _summary,
        http_post=poster,
    )
    notifier.start()
    time.sleep(0.35)  # ~3 ticks
    notifier.stop()
    assert len(poster.calls) >= 2, f"expected ≥2 ticks, got {len(poster.calls)}"
    # All posted titles should be progress, not start/finish.
    assert all("Title" in c["headers"] for c in poster.calls)


def test_http_failure_does_not_raise_or_stop_loop():
    """A flaky ntfy server must never abort the migration."""
    def _flaky(*args, **kwargs):
        raise OSError("network down")
    notifier = ProgressNotifier(
        NtfyConfig(topic_url="https://ntfy.sh/t", interval_seconds=0.05),
        lambda: {"completed": 0, "pending": 1, "in_progress": 0, "failed": 0, "exhausted": 0},
        http_post=_flaky,
    )
    notifier._started_at = 0.0
    # Should not raise — just logs the warning.
    notifier._post_progress()
    notifier.notify_fatal_error("anything")
    notifier.notify_run_started("2020-01-01", "2020-01-31", 1)


def test_non_2xx_status_logs_but_does_not_raise():
    poster = _FakePoster(status=500)
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    notifier.notify_run_started("2020-01-01", "2020-01-31", 1)
    # No exception. Just one POST attempt logged.
    assert len(poster.calls) == 1


# ---------------------------------------------------------------------------
# Header encoding edge cases
# ---------------------------------------------------------------------------


def test_non_ascii_title_is_stripped_for_header_safety():
    """ntfy titles go through HTTP headers which are ASCII-only.
    Emoji or smart-quotes in the title must not raise."""
    poster = _FakePoster()
    notifier = ProgressNotifier(_config(), lambda: {}, http_post=poster)
    # Inject a non-ASCII title via the private helper to keep the test focused.
    notifier._post(title="CJEU done ✅ — hooray", message="ok", tags=("check",))
    assert "?" in poster.calls[0]["headers"]["Title"]  # non-ASCII replaced
