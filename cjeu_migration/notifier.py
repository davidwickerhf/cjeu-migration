"""ntfy.sh progress notifier.

When ``NTFY_TOPIC_URL`` is set, a background thread posts the manifest
summary every ``NTFY_INTERVAL_SECONDS`` (default 1800 = 30 min) so the user
gets a phone push without staying SSH'd in. The notifier also fires explicit
events on start / scrape-done / push-done / fatal error.

Design notes:

- Daemon thread, never joins on shutdown beyond a tiny timeout — a hung HTTP
  POST must not block the main pipeline.
- HTTP failures are logged but never raised. A notifier outage shouldn't
  abort a 15-hour data migration.
- Stateless w.r.t. retries: each tick re-reads the manifest fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib import request as urlrequest
from urllib.error import URLError


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NtfyConfig:
    topic_url: str
    interval_seconds: int = 1800
    auth_token: Optional[str] = None


# Type alias for the small HTTP function we factor out (lets tests inject a fake).
HttpPostFn = Callable[[str, bytes, dict, float], int]


def _default_http_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    """Minimal POST wrapper around urllib. Returns the HTTP status code."""
    req = urlrequest.Request(url, data=body, method="POST", headers=headers)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.status


class ProgressNotifier:
    """Background-thread ntfy poster + explicit event API.

    ``manifest_summary_fn`` is the source of truth for what to send each tick
    — typically ``manifest.summary`` (a callable returning ``Dict[str, int]``).
    Passing it as a callable rather than the manifest itself keeps coupling
    minimal and tests trivial.
    """

    def __init__(
        self,
        config: NtfyConfig,
        manifest_summary_fn: Callable[[], dict],
        *,
        http_post: HttpPostFn = _default_http_post,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._summary_fn = manifest_summary_fn
        self._http_post = http_post
        self._timeout = timeout_seconds
        self._clock = clock
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: float = 0.0

    # --------------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------------

    def start(self) -> None:
        """Begin posting periodic progress updates in a background thread."""
        self._started_at = self._clock()
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit and wait briefly for it to do so."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # --------------------------------------------------------------
    # Explicit events
    # --------------------------------------------------------------

    def notify_run_started(self, start_date: str, end_date: str, total_windows: int) -> None:
        self._post(
            title=f"CJEU migration started ({total_windows} windows)",
            message=(
                f"Window range: {start_date} → {end_date}\n"
                f"Total windows: {total_windows}\n"
                f"Reporting interval: {self._format_duration(self._config.interval_seconds)}"
            ),
            tags=("rocket",),
            priority="default",
        )

    def notify_run_finished(self, summary: dict, cases_rows: int, fulltexts_rows: int) -> None:
        completed = summary.get("completed", 0)
        exhausted = summary.get("exhausted", 0)
        total = sum(summary.values())
        elapsed = self._clock() - self._started_at
        if exhausted == 0 and completed == total:
            title = "CJEU migration done ✅"
            tags = ("white_check_mark",)
            priority = "high"
        elif exhausted > 0:
            title = f"CJEU migration done with {exhausted} exhausted window(s) ⚠️"
            tags = ("warning",)
            priority = "high"
        else:
            title = "CJEU migration ended"
            tags = ("checkered_flag",)
            priority = "default"
        self._post(
            title=title,
            message=(
                f"Completed: {completed}/{total}\n"
                f"Exhausted: {exhausted}\n"
                f"Failed (transient): {summary.get('failed', 0)}\n"
                f"Cases rows: {cases_rows:,}\n"
                f"Fulltext rows: {fulltexts_rows:,}\n"
                f"Total time: {self._format_duration(elapsed)}"
            ),
            tags=tags,
            priority=priority,
        )

    def notify_fatal_error(self, error: str) -> None:
        self._post(
            title="CJEU migration: fatal error ❌",
            message=str(error)[:1500],
            tags=("rotating_light",),
            priority="urgent",
        )

    # --------------------------------------------------------------
    # Periodic tick
    # --------------------------------------------------------------

    def _tick_loop(self) -> None:
        # First tick after one interval, not immediately — the start event
        # already covers the launch moment.
        while not self._stop_event.wait(self._config.interval_seconds):
            self._post_progress()

    def _post_progress(self) -> None:
        try:
            summary = self._summary_fn()
        except Exception as exc:
            log.warning("notifier: manifest read failed: %s", exc)
            return
        completed = summary.get("completed", 0)
        total = sum(summary.values())
        elapsed = self._clock() - self._started_at
        eta = self._estimate_eta(completed, total, elapsed)
        title = f"CJEU migration: {completed}/{total} windows"
        message = (
            f"Completed: {completed}/{total}\n"
            f"In progress: {summary.get('in_progress', 0)}\n"
            f"Failed (retrying): {summary.get('failed', 0)}\n"
            f"Exhausted: {summary.get('exhausted', 0)}\n"
            f"Pending: {summary.get('pending', 0)}\n"
            f"Elapsed: {self._format_duration(elapsed)}\n"
            f"ETA: {eta}"
        )
        self._post(title=title, message=message, tags=("chart_with_upwards_trend",))

    # --------------------------------------------------------------
    # Low-level POST
    # --------------------------------------------------------------

    def _post(
        self,
        *,
        title: str,
        message: str,
        tags: Iterable[str] = (),
        priority: str = "default",
    ) -> None:
        headers = {
            "Title": _ascii_header(title),
            "Tags": ",".join(tags),
            "Priority": priority,
        }
        if self._config.auth_token:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"
        try:
            status = self._http_post(
                self._config.topic_url,
                message.encode("utf-8"),
                headers,
                self._timeout,
            )
            if status >= 400:
                log.warning("ntfy POST returned HTTP %d", status)
        except (URLError, OSError, TimeoutError) as exc:
            log.warning("ntfy POST failed (non-fatal): %s", exc)

    # --------------------------------------------------------------
    # Formatting helpers
    # --------------------------------------------------------------

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = int(max(0, seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h{minutes:02d}m"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"

    def _estimate_eta(self, completed: int, total: int, elapsed: float) -> str:
        if completed == 0 or total == 0:
            return "—"
        per_window = elapsed / completed
        remaining = max(0, total - completed) * per_window
        return self._format_duration(remaining)


def _ascii_header(value: str) -> str:
    """Strip non-ASCII so the HTTP header can be encoded.

    ntfy's web client tolerates UTF-8 in titles but the underlying urllib
    header builder is ASCII-only. Replace anything else with a ``?``.
    """
    return value.encode("ascii", errors="replace").decode("ascii")
