"""Configuration loading. Reads .env then env vars, applies defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv


WindowKind = Literal["month", "quarter", "year"]


def _str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_date(env_var: str, value: str) -> date:
    """Parse a date with a friendly error message when the .env value is bogus."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{env_var}={value!r} is not a valid ISO date (expected YYYY-MM-DD, "
            f"and the date must really exist on the calendar). Common mistakes: "
            f"Feb 30, leap-day in a non-leap year, slashes instead of dashes, "
            f"DD-MM-YYYY order. Original error: {exc}"
        ) from exc


def _clean_token(value: Optional[str]) -> Optional[str]:
    """Strip whitespace and non-ASCII chars from a token loaded from .env.

    HuggingFace's web UI occasionally lets you copy invisible Unicode
    separators (``\\u2028``, ``\\u00a0``, …) into the token field. These pass
    visual inspection but break the HTTP ``Authorization`` header at the
    ASCII-encoding step deep inside httpx. Defensive scrub.
    """
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if 32 <= ord(ch) < 127).strip()
    return cleaned or None


def _parse_int(env_var: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{env_var}={value!r} is not a valid integer. Check your .env file."
        ) from exc


@dataclass(frozen=True)
class Config:
    """Runtime configuration. Construct via :meth:`from_env`."""

    hf_token: Optional[str]
    hf_dataset_repo: str
    workspace_dir: Path
    start_date: date
    end_date: date
    window: WindowKind
    max_ecli_per_window: int
    extractor_threads: int
    max_window_retries: int
    skip_upload: bool
    ntfy_topic_url: Optional[str]
    ntfy_interval_seconds: int
    ntfy_auth_token: Optional[str]

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Config":
        if env_file is None:
            env_file = Path(".env")
        if env_file.exists():
            load_dotenv(env_file, override=False)

        start_date = _parse_date("START_DATE", os.environ.get("START_DATE", "1954-01-01").strip())
        end_raw = os.environ.get("END_DATE", "").strip()
        end_date = _parse_date("END_DATE", end_raw) if end_raw else date.today()

        return cls(
            hf_token=_clean_token(os.environ.get("HUGGINGFACE_TOKEN")),
            hf_dataset_repo=os.environ.get("HF_DATASET_REPO", "").strip()
                or "maastrichtlawtech/cjeu-cases",
            workspace_dir=Path(os.environ.get("WORKSPACE_DIR", "./workspace")).resolve(),
            start_date=start_date,
            end_date=end_date,
            window=os.environ.get("WINDOW", "month").strip().lower(),  # type: ignore[arg-type]
            max_ecli_per_window=_parse_int("MAX_ECLI_PER_WINDOW", os.environ.get("MAX_ECLI_PER_WINDOW", "10000")),
            extractor_threads=_parse_int("EXTRACTOR_THREADS", os.environ.get("EXTRACTOR_THREADS", "10")),
            max_window_retries=_parse_int("MAX_WINDOW_RETRIES", os.environ.get("MAX_WINDOW_RETRIES", "3")),
            skip_upload=_str_to_bool(os.environ.get("SKIP_UPLOAD", "0")),
            ntfy_topic_url=(os.environ.get("NTFY_TOPIC_URL") or "").strip() or None,
            ntfy_interval_seconds=_parse_int(
                "NTFY_INTERVAL_SECONDS", os.environ.get("NTFY_INTERVAL_SECONDS", "1800")
            ),
            ntfy_auth_token=(os.environ.get("NTFY_AUTH_TOKEN") or "").strip() or None,
        )

    @property
    def cases_dir(self) -> Path:
        """Per-window case CSV files land here."""
        return self.workspace_dir / "windows" / "cases"

    @property
    def fulltexts_dir(self) -> Path:
        """Per-window fulltext JSON files land here."""
        return self.workspace_dir / "windows" / "fulltexts"

    @property
    def manifest_path(self) -> Path:
        return self.workspace_dir / "manifest.json"

    @property
    def consolidated_dir(self) -> Path:
        """Final parquet outputs land here, ready to push to HF."""
        return self.workspace_dir / "dataset"
