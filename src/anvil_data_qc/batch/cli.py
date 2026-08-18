"""``qc-batch-runner`` — a separate CLI (a meta-pipeline) that batch-runs a QC pipeline over a snapshot
list via the Terra submissions API, capped at a small concurrency.

    qc-batch-runner --tier prod --table anvil15_qc --pipeline ade_qc --snapshots priorityA.tsv \
        [--max-concurrent 5] [--dry-run] [--force] [--run-skipped] \
        [--on-stale-token auto|prompt|wait] \
        [--qc-workspace-name anvil-data-qc-shared] [--config config/config.yaml]

Reuses the repo's config, identity guard, Azul-token validation, logging, and redaction. The per-dataset
QC verdicts land in the data-table columns as usual; this tool orchestrates and reports. Deliberately a
separate tool/CLI from ``anvil-data-qc`` (a distinct layer). Kept Python 3.10-compatible.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Optional

import typer

from .. import __version__
from ..auth import IdentityMismatchError, assert_tier_identity, bearer_token, credentials_for_tier
from ..config import load_config, resolve_tier
from ..logging_setup import LOGGER_NAME, setup_logging, write_header
from .runner import (
    DEFAULT_MAX_CONCURRENT,
    PIPELINE_METHOD_CONFIG,
    BatchParams,
    RunnerFatalError,
    run_batch,
)
from .terra import TerraWorkspaceClient

logger = logging.getLogger(LOGGER_NAME)

_STALE_TOKEN_MODES = ("auto", "prompt", "wait")
SNAPSHOT_ID_COLUMN = "snapshot_id"


def _read_snapshot_ids(tsv_path: Path) -> list:
    """Read the ``snapshot_id`` column from a TSV (order-preserving, de-duplicated); fail-fast on issues."""
    if not tsv_path.is_file():
        raise RunnerFatalError(f"Snapshot list not found: {tsv_path}")
    with tsv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if not reader.fieldnames or SNAPSHOT_ID_COLUMN not in reader.fieldnames:
            raise RunnerFatalError(
                f"{tsv_path} has no {SNAPSHOT_ID_COLUMN!r} column (columns: {reader.fieldnames})."
            )
        seen, ids = set(), []
        for row in reader:
            sid = (row.get(SNAPSHOT_ID_COLUMN) or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
    if not ids:
        raise RunnerFatalError(f"No snapshot_ids found in {tsv_path}.")
    return ids


def main(
    pipeline: str = typer.Option(
        ..., "--pipeline", "-p", help=f"QC pipeline to run: one of {list(PIPELINE_METHOD_CONFIG)}."
    ),
    table: str = typer.Option(
        ..., "--table", help="Root entity type = the Terra data table name (e.g. anvil15_qc)."
    ),
    snapshots: Path = typer.Option(
        ..., "--snapshots", "-s", help="TSV file with a 'snapshot_id' column (the rows to process)."
    ),
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t", help="Tier (dev|prod). Defaults to the config's default_tier."
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
    qc_workspace_name: Optional[str] = typer.Option(
        None,
        "--qc-workspace-name",
        help="Override the config's qc_workspace_name (the shared QC workspace to submit into).",
    ),
    max_concurrent: int = typer.Option(
        DEFAULT_MAX_CONCURRENT, "--max-concurrent", help="Max concurrent workflows (default 5)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate + print the effective plan; create no submissions."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "--rerun-passed",
        help="Re-run rows that already passed (default: skip them).",
    ),
    run_skipped: bool = typer.Option(
        False,
        "--run-skipped",
        help="Also run rows marked to skip (non-empty 'qc_skip' column); default: exclude them.",
    ),
    on_stale_token: str = typer.Option(
        "auto",
        "--on-stale-token",
        help="ade_qc token handling when TTL is low: auto (TTY→prompt else wait) | prompt | wait.",
    ),
) -> None:
    """Batch-run a QC pipeline over a snapshot list, capped at --max-concurrent, results in the data table."""
    if pipeline not in PIPELINE_METHOD_CONFIG:
        raise typer.BadParameter(f"--pipeline must be one of {list(PIPELINE_METHOD_CONFIG)}.")
    if on_stale_token not in _STALE_TOKEN_MODES:
        raise typer.BadParameter(f"--on-stale-token must be one of {list(_STALE_TOKEN_MODES)}.")

    cfg = load_config(config)
    resolved = resolve_tier(cfg, tier)
    workspace_name = qc_workspace_name or resolved.qc_workspace_name
    if not workspace_name:
        raise typer.BadParameter(
            "No QC workspace name: set qc_workspace_name in the tier config or pass --qc-workspace-name."
        )

    _logger, log_file = setup_logging(Path("logs"), resolved.name, f"batch-{pipeline}")
    snapshot_ids = _read_snapshot_ids(snapshots)
    write_header(
        _logger,
        version=__version__,
        tool="qc-batch-runner",
        tier=resolved.name,
        workspace=f"{resolved.terra_billing_project}/{workspace_name}",
        pipeline=pipeline,
        table=table,
        snapshot_count=len(snapshot_ids),
        max_concurrent=max_concurrent,
        dry_run=dry_run,
        force=force,
        run_skipped=run_skipped,
        on_stale_token=on_stale_token,
        log_file=str(log_file),
    )

    creds = credentials_for_tier(resolved)

    def google_token() -> str:
        return bearer_token(creds)

    try:
        assert_tier_identity(creds, resolved)
        client = TerraWorkspaceClient(
            resolved.url("firecloud"), google_token, resolved.terra_billing_project, workspace_name
        )
        params = BatchParams(
            pipeline=pipeline,
            entity_type=table,
            snapshot_ids=snapshot_ids,
            max_concurrent=max_concurrent,
            dry_run=dry_run,
            force=force,
            run_skipped=run_skipped,
            on_stale_token=on_stale_token,
        )
        result = run_batch(client=client, creds=creds, tier=resolved, params=params)
    except (RunnerFatalError, IdentityMismatchError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc

    typer.echo(result.render())  # end-of-run summary → stdout
    raise typer.Exit(result.exit_code)


def run() -> None:
    """Console-script entrypoint (flat CLI via ``typer.run``)."""
    typer.run(main)


if __name__ == "__main__":
    run()
