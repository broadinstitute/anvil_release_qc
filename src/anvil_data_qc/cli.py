"""Command-line entrypoint (Typer).

anvil-data-qc qc --snapshot-id <id> [--tier dev|prod] [--catalog <cat>] [--dry-run] [--config path]
anvil-data-qc compare --snapshot-id <id> --workspace-namespace <ns> --workspace-name <name> [...]
anvil-data-qc compare-ingest --submitter-workspace-namespace <ns> --submitter-workspace-name <name> \
    --snapshot-id <id> [--tier dev|prod] [--config path]
anvil-data-qc azul-login [--tier dev|prod] [--config path] [--from-curl] [--no-open]
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer

from .auth import assert_tier_identity, bearer_token, credentials_for_tier
from .azul_auth import (
    AzulTokenError,
    expected_client_id,
    extract_bearer_token,
    validate_azul_token,
    write_azul_token,
)
from .clients.tdr import TdrClient, snapshot_bigquery_access
from .compare.ingest import run_ingest_comparison
from .compare.qc import run_qc_comparison
from .config import load_config, resolve_tier
from .pipeline import run_qc
from .safety import SafetyError

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
    try:
        result = run_qc(
            snapshot_id=snapshot_id,
            tier_name=tier,
            config_path=config,
            dry_run=dry_run,
            catalog=catalog,
        )
    except SafetyError as exc:
        # Safety gate (consent / signed-URL hand-off; see safety.py). Clean message, no traceback.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if (
        result.get("qc_passed") is False
    ):  # absent on dry runs; only a real QC failure exits non-zero
        raise typer.Exit(1)


@app.command("compare")
def compare(
    snapshot_id: str = typer.Option(
        ..., "--snapshot-id", "-s", help="TDR snapshot id (the source of truth)."
    ),
    workspace_namespace: str = typer.Option(
        ..., "--workspace-namespace", help="AW namespace (Terra billing project)."
    ),
    workspace_name: str = typer.Option(..., "--workspace-name", help="AW workspace name."),
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t", help="Tier (dev|prod). Defaults to the config's default_tier."
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
) -> None:
    """Compare an existing AW's data tables to a TDR snapshot (T1-T2 + auth-domain); no import.

    Useful to QC a workspace a prior run created, or to re-run QC without re-importing. Exits non-zero
    if the comparison fails.
    """
    cfg = load_config(config)
    resolved = resolve_tier(cfg, tier)
    creds = credentials_for_tier(resolved)
    assert_tier_identity(creds, resolved)

    def google_token() -> str:
        return bearer_token(creds)

    try:
        snapshot = TdrClient(resolved.url("tdr"), google_token).retrieve_snapshot(snapshot_id)
        bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
        result = run_qc_comparison(
            creds=creds,
            tier=resolved,
            snapshot_id=snapshot_id,
            bq_project=bq_project,
            bq_dataset=bq_dataset,
            namespace=workspace_namespace,
            workspace_name=workspace_name,
            token_provider=google_token,
        )
    except SafetyError as exc:
        # Safety gate (e.g. a bearer-token destination violation; see safety.py). Clean message.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    typer.echo(result.render())
    raise typer.Exit(0 if result.passed else 1)


@app.command("compare-ingest")
def compare_ingest(
    submitter_workspace_namespace: str = typer.Option(
        ..., "--submitter-workspace-namespace", help="Submitter (ADSW) workspace namespace."
    ),
    submitter_workspace_name: str = typer.Option(
        ..., "--submitter-workspace-name", help="Submitter (ADSW) workspace name."
    ),
    snapshot_id: str = typer.Option(
        ...,
        "--snapshot-id",
        "-s",
        help="TDR snapshot id (the system of record to compare against).",
    ),
    tier: Optional[str] = typer.Option(
        None, "--tier", "-t", help="Tier (dev|prod). Defaults to the config's default_tier."
    ),
    config: Path = typer.Option(
        Path("config/config.yaml"), "--config", "-c", help="Path to the YAML config file."
    ),
) -> None:
    """Compare a submitter (ADSW) workspace's tables to the TDR snapshot (ingestion QC).

    Verifies the ADSW -> TDR step: every table the submitter provided was ingested into TDR with the
    same row count. Driven from the submitter workspace; TDR's harmonized anvil_* / file_inventory /
    workspace_attributes tables are out of scope. No PFB export (no Azul / signed URL / consent gate).
    Exits non-zero if any submitter table is missing from TDR or has a row-count mismatch.
    """
    cfg = load_config(config)
    resolved = resolve_tier(cfg, tier)
    creds = credentials_for_tier(resolved)
    assert_tier_identity(creds, resolved)

    def google_token() -> str:
        return bearer_token(creds)

    try:
        snapshot = TdrClient(resolved.url("tdr"), google_token).retrieve_snapshot(snapshot_id)
        bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
        report = run_ingest_comparison(
            creds=creds,
            tier=resolved,
            bq_project=bq_project,
            bq_dataset=bq_dataset,
            submitter_namespace=submitter_workspace_namespace,
            submitter_workspace_name=submitter_workspace_name,
            token_provider=google_token,
        )
    except SafetyError as exc:
        # Safety gate (e.g. a bearer-token destination violation; see safety.py). Clean message.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    typer.echo(report.render())
    raise typer.Exit(0 if report.passed else 1)


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
