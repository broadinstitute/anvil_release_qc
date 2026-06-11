"""Command-line entrypoint (Typer).

    anvil-data-qc qc --snapshot-id <tdr-snapshot-id> [--tier dev|prod] [--config path]
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .pipeline import run_qc

app = typer.Typer(
    help="AnVIL Data Release QC: export a TDR snapshot to a Terra workspace and verify it.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """AnVIL Data Release QC commands."""


@app.command()
def qc(
    snapshot_id: str = typer.Option(
        ..., "--snapshot-id", "-s", help="TDR snapshot id (the input / system of record)."
    ),
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t", help="Tier to run against (dev|prod). Defaults to the config's default_tier."
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
) -> None:
    """Run the export/import + QC flow for one TDR snapshot."""
    run_qc(snapshot_id=snapshot_id, tier_name=tier, config_path=config)


if __name__ == "__main__":
    app()
