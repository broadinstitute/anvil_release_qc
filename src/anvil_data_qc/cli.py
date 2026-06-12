"""Command-line entrypoint (Typer).

anvil-data-qc qc --snapshot-id <tdr-snapshot-id> [--tier dev|prod] [--config path]
anvil-data-qc azul-login [--tier dev|prod] [--config path] [--from-curl] [--no-open]
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from .azul_auth import (
    AzulTokenError,
    expected_client_id,
    extract_bearer_token,
    validate_azul_token,
    write_azul_token,
)
from .config import load_config, resolve_tier
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
        None,
        "--tier",
        "-t",
        help="Tier to run against (dev|prod). Defaults to the config's default_tier.",
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
    catalog: Optional[str] = typer.Option(
        None,
        "--catalog",
        help="Azul catalog (the data release). Omit in dev (uses the default); set in prod, e.g. --catalog anvil14.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the export side only (TDR -> BigQuery -> Azul); create no workspace, run no import.",
    ),
) -> None:
    """Run the export/import + QC flow for one TDR snapshot."""
    run_qc(
        snapshot_id=snapshot_id,
        tier_name=tier,
        config_path=config,
        dry_run=dry_run,
        catalog=catalog,
    )


@app.command("azul-login")
def azul_login(
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t", help="Tier (dev|prod). Defaults to the config's default_tier."
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
    from_curl: bool = typer.Option(
        False,
        "--from-curl",
        help="Read the Swagger 'curl' command from stdin and extract the bearer token.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the Azul Swagger UI in a browser (like gcloud auth login).",
    ),
) -> None:
    """Capture an Azul access token via interactive browser login and store it for the tier.

    Azul needs a Google access token issued to *its own* OAuth client (a plain gcloud ADC token is
    rejected). This opens the Azul Swagger UI, you sign in and copy the token, and it is validated
    (azp, account, expiry) and written to the tier's token file. See docs/export_import_flow.md.
    """
    cfg = load_config(config)
    resolved = resolve_tier(cfg, tier)
    swagger_url = resolved.url("azul").rstrip("/") + "/"
    client_id = expected_client_id(resolved)

    typer.echo(f"Azul login for tier '{resolved.name}' as {resolved.email}:")
    typer.echo(f"  1. In the Azul Swagger UI, click Authorize and sign in as {resolved.email}.")
    typer.echo("  2. Expand any GET operation, click 'Try it out', then 'Execute'.")
    typer.echo("  3. In the 'Curl' box, copy the value after 'Authorization: Bearer'.")
    if open_browser:
        webbrowser.open(swagger_url)
    else:
        typer.echo(f"  (open: {swagger_url})")

    if from_curl:
        typer.echo("Paste the curl command, then press Ctrl-D:")
        raw = sys.stdin.read()
    else:
        raw = typer.prompt("Paste the Azul access token (or the Swagger curl line)")

    try:
        token = extract_bearer_token(raw)
        info = validate_azul_token(token, expected_azp=client_id, expected_email=resolved.email)
        path = write_azul_token(resolved, token, info)
    except AzulTokenError as exc:
        typer.secho(f"Token rejected: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    minutes = int(info.get("expires_in", 0)) // 60
    typer.secho(
        f"Stored token for {info.get('email')} at {path} (expires in ~{minutes} min).",
        fg=typer.colors.GREEN,
    )


if __name__ == "__main__":
    app()
