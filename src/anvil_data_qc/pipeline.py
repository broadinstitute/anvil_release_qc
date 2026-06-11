"""End-to-end QC pipeline orchestration.

Implements the export/import flow from docs/export_import_flow.md:

    TDR retrieveSnapshot -> BigQuery anvil_dataset.dataset_id -> Azul manifest (verbatim.pfb, poll)
    -> Rawls createWorkspace (authorizationDomain: []) -> Firecloud importJob -> poll to success.

The QC verifications (auth-domain match, table comparison) are the next layer and are not yet
implemented. End-to-end execution requires real per-tier ADC credentials and a real TDR snapshot, and
creates a real (retained) workspace -- run it against the dev tier.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import __version__
from .auth import assert_tier_identity, credentials_for_tier
from .bigquery import resolve_dataset_id
from .clients.azul import AzulClient
from .clients.firecloud import FirecloudClient, import_job_id
from .clients.rawls import RawlsClient
from .clients.tdr import TdrClient, snapshot_bigquery_access, snapshot_name
from .config import load_config, resolve_tier
from .logging_setup import LOGGER_NAME, setup_logging, write_header
from .workspace import workspace_name

logger = logging.getLogger(LOGGER_NAME)


def run_qc(
    snapshot_id: str,
    tier_name: Optional[str],
    config_path: Path,
    log_dir: Path = Path("logs"),
) -> dict:
    """Run the export/import flow for one TDR snapshot. Returns the created workspace + log path."""
    config = load_config(config_path)
    tier = resolve_tier(config, tier_name)

    _logger, log_file = setup_logging(log_dir, tier.name, snapshot_id)
    write_header(
        _logger,
        version=__version__,
        tier=tier.name,
        snapshot_id=snapshot_id,
        billing_project=tier.billing_project,
        log_file=str(log_file),
    )

    creds = credentials_for_tier(tier)
    assert_tier_identity(creds, tier)

    # 1. TDR: retrieve the snapshot (name + BigQuery access info).
    tdr = TdrClient(tier.url("tdr"), creds)
    snapshot = tdr.retrieve_snapshot(snapshot_id)
    name = snapshot_name(snapshot)
    bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
    logger.info("Snapshot '%s' -> BigQuery %s.%s", name, bq_project, bq_dataset)

    # 2. BigQuery: resolve the Azul dataset_id from the snapshot (system of record).
    dataset_id = resolve_dataset_id(creds, bq_project, bq_dataset, tier.billing_project)

    # 3. Azul: generate the PFB manifest and obtain the signed S3 URL.
    azul = AzulClient(tier.url("azul"), creds)
    signed_url = azul.request_pfb_manifest(dataset_id)

    # 4. Rawls: create the destination workspace (empty auth domain; import applies it).
    ws_name = workspace_name(tier.email, name)
    rawls = RawlsClient(tier.url("rawls"), creds)
    logger.info("Creating workspace %s/%s", tier.billing_project, ws_name)
    rawls.create_workspace(
        tier.billing_project, ws_name, description=f"AnVIL Data Release QC export of {name}"
    )

    # 5. Firecloud: import the PFB and wait for the job to succeed.
    firecloud = FirecloudClient(tier.url("firecloud"), creds)
    submit = firecloud.submit_import_job(tier.billing_project, ws_name, signed_url)
    job_id = import_job_id(submit)
    logger.info("Import job submitted: %s", job_id)
    firecloud.wait_for_import(tier.billing_project, ws_name, job_id)

    logger.info("Export/import complete. Workspace: %s/%s", tier.billing_project, ws_name)
    logger.info("QC verifications (auth-domain match, table comparison) are the next layer (TODO).")
    return {
        "workspace_namespace": tier.billing_project,
        "workspace_name": ws_name,
        "snapshot_name": name,
        "log_file": str(log_file),
    }
