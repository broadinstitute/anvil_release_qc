"""End-to-end QC pipeline orchestration (spike shell).

The resolved flow is specified in ``docs/export_import_flow.md``. This module wires the foundational
layers (config, auth + identity guard, logging) for real, and lays out the flow steps. The flow
steps themselves are stubs for now -- the spike fleshes them in next:

    resolve dataset_id (BigQuery anvil_dataset) -> Azul manifest (verbatim.pfb, async poll)
    -> Rawls createWorkspace (authorizationDomain: []) -> Firecloud importJob -> poll to success
    -> QC verifications (auth-domain match, table comparison).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import __version__
from .auth import assert_tier_identity, credentials_for_tier
from .config import AppConfig, ResolvedTier, load_config, resolve_tier
from .logging_setup import LOGGER_NAME, setup_logging, write_header

logger = logging.getLogger(LOGGER_NAME)


def run_qc(
    snapshot_id: str,
    tier_name: Optional[str],
    config_path: Path,
    log_dir: Path = Path("logs"),
) -> None:
    """Run the export/import + QC flow for one TDR snapshot.

    Foundations (config, logging, auth + identity guard) are real; the flow steps are not yet
    implemented (spike shell) and raise ``NotImplementedError``.
    """
    config: AppConfig = load_config(config_path)
    tier: ResolvedTier = resolve_tier(config, tier_name)

    _logger, log_file = setup_logging(log_dir, tier.name, snapshot_id)
    write_header(
        _logger,
        version=__version__,
        tier=tier.name,
        snapshot_id=snapshot_id,
        billing_project=tier.billing_project,
        log_file=str(log_file),
    )

    # Auth + identity guard (real): never act on the wrong tier.
    creds = credentials_for_tier(tier)
    assert_tier_identity(creds, tier)

    # --- Flow steps: TODO, implement during the spike. See docs/export_import_flow.md. ---
    logger.info("Resolving the Azul dataset_id from the snapshot's anvil_dataset table (BigQuery)...")
    raise NotImplementedError(
        "Export/import flow steps are not implemented yet (spike shell). Next: resolve dataset_id "
        "(BigQuery) -> Azul manifest -> createWorkspace -> importJob -> poll. "
        "See docs/export_import_flow.md."
    )
