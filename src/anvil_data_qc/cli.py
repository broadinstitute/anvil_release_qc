"""Command-line entrypoint (Typer).

anvil-data-qc ade-qc --snapshot-id <id> [--tier dev|prod] [--catalog <cat>] [--dry-run] [--verify-auth] [--config path]
anvil-data-qc tdr-qc --snapshot-id <id> [--tier dev|prod] [--dry-run] [--verify-auth] [--config path]
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
    AzulReindexingError,
    AzulTokenError,
    expected_client_id,
    extract_bearer_token,
    validate_azul_token,
    write_azul_token,
)
from .bigquery import BigQueryAccessError, check_bigquery_access, snapshot_data_volume
from .clients.tdr import TdrClient, snapshot_bigquery_access
from .compare.ingest import run_ingest_comparison
from .compare.profile import TDR_SYSTEM_TABLES
from .compare.qc import run_qc_comparison
from .config import load_config, resolve_tier
from .pipeline import run_ade_qc, run_tdr_qc
from .safety import SafetyError

app = typer.Typer(
    help="AnVIL Data Release QC: export a TDR snapshot to a Terra workspace and verify it.",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """AnVIL Data Release QC commands."""


@app.command("ade-qc")
def ade_qc(
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
        help="Azul catalog (the data release). Omit in dev (uses the default); set in prod, e.g. --catalog anvil15.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the export side only (TDR -> BigQuery -> Azul); create no workspace, run no import.",
    ),
    allow_controlled_access: bool = typer.Option(
        False,
        "--allow-controlled-access",
        help="Opt in to controlled-access snapshots (any consentCode). Default off = open-access only.",
    ),
    azul_token_file: Optional[str] = typer.Option(
        None,
        "--azul-token-file",
        help="Path to an Azul token file, overriding the tier's azul_token_file (used by the WDL).",
    ),
    verify_auth: bool = typer.Option(
        False,
        "--verify-auth",
        help="Auth preflight: verify identity + Azul token + snapshot access + consent + BigQuery "
        "access, then exit (no export, no signed URL). A quick check before committing to a run.",
    ),
) -> None:
    """Run the ADE/Azul export/import + QC flow for one TDR snapshot."""
    try:
        result = run_ade_qc(
            snapshot_id=snapshot_id,
            tier_name=tier,
            config_path=config,
            dry_run=dry_run,
            catalog=catalog,
            allow_controlled_access=allow_controlled_access,
            azul_token_file=azul_token_file,
            verify_auth=verify_auth,
        )
    except (SafetyError, BigQueryAccessError, AzulReindexingError) as exc:
        # Safety gate (consent / signed-URL hand-off), BigQuery preflight, or Azul-reindexing preflight
        # failure -- see safety.py / bigquery.check_bigquery_access / azul_auth.assert_azul_not_reindexing.
        # All are "cannot run now" aborts (exit 2, retryable), not a QC verdict. Clean message, no traceback.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if (
        result.get("qc_passed") is False
    ):  # absent on dry runs; only a real QC failure exits non-zero
        raise typer.Exit(1)


@app.command("tdr-qc")
def tdr_qc(
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
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the export side only (TDR snapshot export); create no workspace, run no import.",
    ),
    allow_controlled_access: bool = typer.Option(
        False,
        "--allow-controlled-access",
        help="Opt in to controlled-access snapshots (any consentCode). Default off = open-access only.",
    ),
    verify_auth: bool = typer.Option(
        False,
        "--verify-auth",
        help="Auth preflight: verify identity + snapshot access + consent + BigQuery access, then exit "
        "(no export, no signed URL). A quick check before committing to a run.",
    ),
) -> None:
    """Run the TDR direct-export + QC flow for one TDR snapshot.

    Exports the snapshot directly from TDR (no ADE/Azul, no Azul token), imports it into a new Analysis
    Workspace, and verifies it (AW vs snapshot + auth-domain) -- for very large datasets that fail to
    export via ADE/Azul. Open-access by default; controlled-access via --allow-controlled-access (no
    Azul token needed -- your own Terra credentials must grant access to the controlled data).
    """
    try:
        result = run_tdr_qc(
            snapshot_id=snapshot_id,
            tier_name=tier,
            config_path=config,
            dry_run=dry_run,
            allow_controlled_access=allow_controlled_access,
            verify_auth=verify_auth,
        )
    except (SafetyError, BigQueryAccessError) as exc:
        # Safety gate (consent / signed-URL hand-off) or BigQuery preflight failure -- see safety.py /
        # bigquery.check_bigquery_access. Clean message, no traceback.
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
        check_bigquery_access(creds, resolved.billing_google_project_id)
        snapshot = TdrClient(resolved.url("tdr"), google_token).retrieve_snapshot(snapshot_id)
        bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
        volume = snapshot_data_volume(
            creds,
            bq_project,
            bq_dataset,
            resolved.billing_google_project_id,
            ignore_tables=TDR_SYSTEM_TABLES,
        )
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
    except (SafetyError, BigQueryAccessError) as exc:
        # Safety gate (e.g. a bearer-token destination violation) or BigQuery preflight failure. Clean message.
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    # Report to stdout (captured as qc_report.txt by the WDL), with the data-volume line at the end so
    # it rides with the report and the WDL can parse it into the volume outputs.
    typer.echo(result.render())
    typer.echo(volume.summary_line())
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
        check_bigquery_access(creds, resolved.billing_google_project_id)
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
    except (SafetyError, BigQueryAccessError) as exc:
        # Safety gate (e.g. a bearer-token destination violation) or BigQuery preflight failure. Clean message.
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
        info = validate_azul_token(
            token, expected_azp=client_id, allowed_emails=resolved.authorized_emails
        )
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
