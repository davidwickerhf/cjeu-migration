"""Per-window state tracking.

The manifest is a JSON file persisted to ``workspace_dir/manifest.json``. It
records, for every (window_id, sd, ed) tuple the runner considers, the latest
status, attempt count, last error message, row counts, and timestamps.

Resumability story:

1. The runner builds the full list of windows from ``Windowing``.
2. For each window it asks the manifest: ``should_process(window_id)``.
3. Completed windows are skipped. Failed-but-under-retry-cap windows are
   retried. Pending and in-progress (crashed mid-run) windows are picked up.
4. After successful extraction the runner calls ``mark_completed`` with the
   row counts; on failure ``mark_failed`` with the error and the attempt
   counter increments.

The manifest is atomically written (temp file + rename) on every change so a
mid-write crash never produces a half-written file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class WindowStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"  # retries used up — manual investigation needed


@dataclass
class WindowState:
    window_id: str
    sd: str  # ISO date string
    ed: str
    status: WindowStatus = WindowStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None
    row_count: Optional[int] = None
    fulltext_count: Optional[int] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WindowState":
        return cls(
            window_id=data["window_id"],
            sd=data["sd"],
            ed=data["ed"],
            status=WindowStatus(data.get("status", WindowStatus.PENDING.value)),
            attempts=int(data.get("attempts", 0)),
            last_error=data.get("last_error"),
            row_count=data.get("row_count"),
            fulltext_count=data.get("fulltext_count"),
            updated_at=data.get("updated_at"),
        )


class Manifest:
    """Persisted state for the orchestration loop.

    Thread-safe: a single lock guards every read-modify-write cycle and the
    file write. Callers should treat ``Manifest`` as a value object owned by
    one process at a time — there's no cross-process locking. On Vast.ai each
    deployment is one process, which is the intended usage.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, windows: Optional[Dict[str, WindowState]] = None):
        self.path = path
        self._lock = threading.Lock()
        self._windows: Dict[str, WindowState] = windows or {}

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        if not path.exists():
            return cls(path=path, windows={})
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        windows = {
            window_id: WindowState.from_dict(state)
            for window_id, state in raw.get("windows", {}).items()
        }
        return cls(path=path, windows=windows)

    def save(self) -> None:
        """Atomically write the manifest to disk."""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "windows": {wid: w.to_dict() for wid, w in self._windows.items()},
        }
        # Write to a temp file in the same directory then atomic-rename.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".manifest.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup of the temp file if rename never happened.
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Bulk planning — populate windows when starting / resuming a run
    # ------------------------------------------------------------------

    def register(self, window_id: str, sd: str, ed: str) -> WindowState:
        """Return the existing state for ``window_id``, or create a pending entry."""
        with self._lock:
            existing = self._windows.get(window_id)
            if existing is not None:
                return existing
            state = WindowState(window_id=window_id, sd=sd, ed=ed)
            self._windows[window_id] = state
            self._save_locked()
            return state

    # ------------------------------------------------------------------
    # Per-window transitions
    # ------------------------------------------------------------------

    def mark_in_progress(self, window_id: str) -> None:
        self._transition(window_id, status=WindowStatus.IN_PROGRESS, bump_attempts=True)

    def mark_completed(
        self, window_id: str, row_count: int, fulltext_count: int
    ) -> None:
        with self._lock:
            state = self._windows[window_id]
            state.status = WindowStatus.COMPLETED
            state.row_count = row_count
            state.fulltext_count = fulltext_count
            state.last_error = None
            state.updated_at = _now_iso()
            self._save_locked()

    def mark_failed(self, window_id: str, error: str, max_retries: int) -> None:
        with self._lock:
            state = self._windows[window_id]
            state.last_error = error
            state.updated_at = _now_iso()
            if state.attempts >= max_retries:
                state.status = WindowStatus.EXHAUSTED
            else:
                state.status = WindowStatus.FAILED
            self._save_locked()

    def _transition(
        self,
        window_id: str,
        *,
        status: WindowStatus,
        bump_attempts: bool = False,
    ) -> None:
        with self._lock:
            state = self._windows[window_id]
            state.status = status
            if bump_attempts:
                state.attempts += 1
            state.updated_at = _now_iso()
            self._save_locked()

    # ------------------------------------------------------------------
    # Orchestration queries
    # ------------------------------------------------------------------

    def should_process(self, window_id: str, max_retries: int) -> bool:
        """Return True when the runner should attempt this window now."""
        state = self._windows.get(window_id)
        if state is None:
            return True
        if state.status == WindowStatus.COMPLETED:
            return False
        if state.status == WindowStatus.EXHAUSTED:
            return False
        # PENDING, IN_PROGRESS (crashed), FAILED — retry if attempts left
        if state.attempts >= max_retries and state.status != WindowStatus.PENDING:
            return False
        return True

    def get(self, window_id: str) -> Optional[WindowState]:
        return self._windows.get(window_id)

    def all_windows(self) -> List[WindowState]:
        return list(self._windows.values())

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {s.value: 0 for s in WindowStatus}
        for w in self._windows.values():
            counts[w.status.value] += 1
        return counts


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
