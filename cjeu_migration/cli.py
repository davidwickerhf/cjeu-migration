"""Command-line entry point.

Two subcommands:

- ``cjeu-migrate run`` — execute the full pipeline.
- ``cjeu-migrate status`` — print the manifest summary without scraping.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from cjeu_migration.config import Config
from cjeu_migration.manifest import Manifest
from cjeu_migration.runner import run as run_pipeline


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )


@click.group()
def main() -> None:
    """CJEU corpus migration tooling."""
    _configure_logging()


@main.command("run")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".env"),
    show_default=True,
    help="Path to the .env file (variables already in the env are kept).",
)
@click.option(
    "--consolidate-only",
    is_flag=True,
    help="Skip the scrape phase; only consolidate previously-saved windows and push.",
)
@click.option(
    "--skip-upload/--no-skip-upload",
    default=None,
    help="Override SKIP_UPLOAD from the env.",
)
def run_cmd(env_file: Path, consolidate_only: bool, skip_upload: bool | None) -> None:
    """Scrape, consolidate, and push."""
    cfg = Config.from_env(env_file)
    if skip_upload is not None:
        cfg = _override_skip_upload(cfg, skip_upload)
    summary = run_pipeline(cfg, consolidate_only=consolidate_only)
    click.echo(
        json.dumps(
            {
                "windows_total": summary.windows_total,
                "windows_completed": summary.windows_completed,
                "windows_failed": summary.windows_failed,
                "windows_exhausted": summary.windows_exhausted,
                "cases_rows": summary.cases_rows,
                "fulltexts_rows": summary.fulltexts_rows,
                "uploaded": summary.uploaded,
            },
            indent=2,
        )
    )
    if summary.windows_exhausted:
        click.echo(
            f"warning: {summary.windows_exhausted} window(s) exhausted retries — "
            f"inspect manifest at {cfg.manifest_path}",
            err=True,
        )
        sys.exit(2)


@main.command("status")
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".env"),
    show_default=True,
)
def status_cmd(env_file: Path) -> None:
    """Print the manifest summary without scraping anything."""
    cfg = Config.from_env(env_file)
    if not cfg.manifest_path.exists():
        click.echo(f"no manifest at {cfg.manifest_path}", err=True)
        sys.exit(1)
    manifest = Manifest.load(cfg.manifest_path)
    summary = manifest.summary()
    click.echo(json.dumps({"summary": summary, "manifest_path": str(cfg.manifest_path)}, indent=2))


def _override_skip_upload(cfg: Config, skip_upload: bool) -> Config:
    """Return a new Config with ``skip_upload`` overridden. Config is frozen."""
    from dataclasses import replace

    return replace(cfg, skip_upload=skip_upload)


if __name__ == "__main__":  # pragma: no cover
    main()
