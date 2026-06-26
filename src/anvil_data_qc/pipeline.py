"""End-to-end QC pipeline orchestration.

Implements the export/import flow from docs/export_import_flow.md:

    TDR retrieveSnapshot -> BigQuery anvil_dataset.dataset_id -> Azul manifest (verbatim.pfb, poll)
    -> Rawls createWorkspace (authorizationDomain: []) -> Firecloud importJob -> poll to success
    -> QC: AW-vs-snapshot table comparison (T1-T2) + auth-domain verification.

End-to-end execution requires real per-tier ADC credentials and a real TDR snapshot, and creates a
real (retained) workspace -- run it against the dev tier.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import __version__
from .auth import assert_tier_identity, bearer_token, credentials_for_tier
from .azul_auth import (
    MIN_TOKEN_SECONDS_FOR_RUN,
    expected_client_id,
    read_azul_token,
    validate_azul_token,
)
from .bigquery import resolve_dataset_id
from .clients.azul import AzulClient
from .clients.firecloud import FirecloudClient, import_job_id
from .clients.rawls import RawlsClient
from .clients.tdr import (
    TdrClient,
    snapshot_bigquery_access,
    snapshot_consent_code,
    snapshot_name,
)
from .compare.qc import run_qc_comparison
from .config import SERVICE_URLS, load_config, resolve_tier
from .logging_setup import LOGGER_NAME, setup_logging, write_header
from .safety import (
    SIGNED_URL_ALLOWED_PREFIXES,
    ControlledAccessNotApprovedError,
    SafetyError,
    assert_consent_permitted,
    is_open_access,
    verify_pfb_handoff,
)
from .timing import stage_timer
from .workspace import workspace_name

logger = logging.getLogger(LOGGER_NAME)


def run_qc(
    snapshot_id: str,
    tier_name: Optional[str],
    config_path: Path,
    log_dir: Path = Path("logs"),
    dry_run: bool = False,
    catalog: Optional[str] = None,
    allow_controlled_access: bool = False,
    azul_token_file: Optional[str] = None,
) -> dict:
    """Run the export/import flow for one TDR snapshot. Returns the created workspace + log path.

    ``dry_run`` stops after Azul returns the signed URL (steps 1-3) -- no workspace is created and no
    import is run -- to validate the export side with zero side effects.

    ``allow_controlled_access`` opts in to running on non-open-access snapshots (default off = fail
    closed, open-access only; see ``safety.assert_consent_permitted``). ``azul_token_file`` overrides
    the tier's configured Azul token path (used by the WDL to pass a localized token file).
    """
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

    def google_token() -> str:
        return bearer_token(creds)

    # Azul token: read + validate up front (fail fast before any TDR/BigQuery work). Azul rejects
    # gcloud ADC tokens, so it needs its own interactive access token (see `azul-login`). With no
    # token configured, Azul is called anonymously -- open-access datasets only.
    azul_token = read_azul_token(tier, override_path=azul_token_file)
    if azul_token is None:
        logger.info(
            "No Azul token configured (azul_token_file unset/empty); calling Azul without a token "
            "(open-access only). Run `anvil-data-qc azul-login` for controlled-access datasets."
        )
    else:
        info = validate_azul_token(
            azul_token,
            expected_azp=expected_client_id(tier),
            allowed_emails=tier.authorized_emails,
            min_remaining_seconds=MIN_TOKEN_SECONDS_FOR_RUN,
        )
        logger.info(
            "Azul token OK: account %s, azp matches, expires in %ss.",
            info.get("email"),
            info.get("expires_in"),
        )

    # 1. TDR: retrieve the snapshot (name + BigQuery access info).
    tdr = TdrClient(tier.url("tdr"), google_token)
    snapshot = tdr.retrieve_snapshot(snapshot_id)
    name = snapshot_name(snapshot)

    # SAFETY GATE (layer 1): only explicitly open-access snapshots proceed UNLESS controlled-access was
    # explicitly authorized for this run (allow_controlled_access). The Azul PFB signed URL grants direct
    # access to an NIH study's controlled-access data; mishandling it (e.g. a prod URL into the insecure
    # dev tier) is a Federal Data Management Incident. Controlled-access is security-team approved but
    # gated behind the explicit opt-in; layers 2-4 (containment, hand-off, bearer-pin) apply regardless.
    consent_code = snapshot_consent_code(snapshot)
    try:
        assert_consent_permitted(
            snapshot_id, consent_code, allow_controlled_access=allow_controlled_access
        )
    except ControlledAccessNotApprovedError as exc:
        logger.error("%s", exc)
        raise
    # Label and enforce off the ACTUAL consentCode (not just the opt-in): an open-access snapshot is
    # never mislabeled "controlled" when the flag is set, and a genuinely controlled snapshot with no
    # Azul token fails fast here (anonymous Azul cannot fetch controlled data) instead of confusingly
    # later at the manifest step.
    if is_open_access(consent_code):
        logger.info("Consent gate OK: snapshot is open-access (consentCode=%r).", consent_code)
    elif azul_token is None:
        exc = SafetyError(
            f"Snapshot {snapshot_id} is controlled-access (consentCode={consent_code!r}) but no Azul "
            "token is available; controlled-access data requires a token (run `anvil-data-qc "
            "azul-login` or pass --azul-token-file). Aborting before any signed URL is generated."
        )
        logger.error("%s", exc)
        raise exc
    else:
        logger.warning(
            "CONTROLLED-ACCESS run AUTHORIZED (allow_controlled_access=True): snapshot %s, "
            "consentCode=%r, tier %s. The PFB signed URL grants direct controlled-data access; "
            "hand-off verification and bearer-pinning still apply.",
            snapshot_id,
            consent_code,
            tier.name,
        )

    bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
    logger.info("Snapshot '%s' -> BigQuery %s.%s", name, bq_project, bq_dataset)

    # 2. BigQuery: resolve the Azul dataset_id from the snapshot (system of record).
    dataset_id = resolve_dataset_id(creds, bq_project, bq_dataset, tier.billing_project)

    # 3. Azul: generate the PFB manifest and obtain the signed S3 URL (token validated above).
    durations: dict[str, float] = {}
    azul = AzulClient(tier.url("azul"), lambda: azul_token, catalog=catalog)
    with stage_timer("azul_manifest", durations):
        signed_url = azul.request_pfb_manifest(dataset_id)

    if dry_run:
        location = signed_url.location  # scheme+host+path only; the signed query stays contained
        logger.info(
            "Dry run: export side validated (TDR -> BigQuery -> Azul). Signed PFB URL at %s. "
            "Skipping workspace creation and import.",
            location,
        )
        return {
            "dry_run": True,
            "snapshot_name": name,
            "bigquery": f"{bq_project}.{bq_dataset}",
            "dataset_id": dataset_id,
            "signed_url_location": location,
            "durations": durations,
            "log_file": str(log_file),
        }

    # 4. Rawls: create the destination workspace (empty auth domain; import applies it).
    ws_name = workspace_name(tier.email, name)
    rawls = RawlsClient(tier.url("rawls"), google_token)
    logger.info("Creating workspace %s/%s", tier.billing_project, ws_name)
    with stage_timer("workspace_create", durations):
        rawls.create_workspace(
            tier.billing_project, ws_name, description=f"AnVIL Data Release QC export of {name}"
        )

    # 5. Firecloud: import the PFB and wait for the job to succeed.
    # Dev Terra can be slow (createWorkspace alone took ~20s); give the import submit/polls headroom.
    firecloud = FirecloudClient(tier.url("firecloud"), google_token, timeout=180)
    # Verify the signed URL is delivered only to this tier's import service (and is from this tier's
    # source) before handing it over -- defense against a controlled-access PFB reaching a wrong/dev
    # destination (a Federal DMI). See safety.verify_pfb_handoff.
    verify_pfb_handoff(
        signed_url,
        tier_name=tier.name,
        expected_firecloud_host=urlsplit(SERVICE_URLS[tier.name]["firecloud"]).netloc,
        destination_host=urlsplit(firecloud.base_url).netloc,
        allowed_prefixes=SIGNED_URL_ALLOWED_PREFIXES.get(tier.name, ()),
    )
    with stage_timer("import", durations):
        submit = firecloud.submit_import_job(tier.billing_project, ws_name, signed_url)
        job_id = import_job_id(submit)
        logger.info("Import job submitted: %s", job_id)
        firecloud.wait_for_import(tier.billing_project, ws_name, job_id)

    logger.info("Export/import complete. Workspace: %s/%s", tier.billing_project, ws_name)
    logger.info(
        "Stage durations — manifest: %.1fs, workspace: %.1fs, import: %.1fs",
        durations.get("azul_manifest", 0.0),
        durations.get("workspace_create", 0.0),
        durations.get("import", 0.0),
    )

    # 6. QC: compare the AW data tables to the TDR snapshot (T1-T2) and verify the auth domain.
    qc = run_qc_comparison(
        creds=creds,
        tier=tier,
        snapshot_id=snapshot_id,
        bq_project=bq_project,
        bq_dataset=bq_dataset,
        namespace=tier.billing_project,
        workspace_name=ws_name,
        token_provider=google_token,
    )
    logger.info("QC report:\n%s", qc.render())
    if not qc.passed:
        logger.warning(
            "QC FAILED for %s/%s -- see the report above.", tier.billing_project, ws_name
        )
    return {
        "workspace_namespace": tier.billing_project,
        "workspace_name": ws_name,
        "snapshot_name": name,
        "qc_passed": qc.passed,
        "durations": durations,
        "log_file": str(log_file),
    }
