"""End-to-end QC pipeline orchestration.

Two export/import flows share the same QC tail; each differs only in how it produces the contained
signed URL handed to the Terra import:

- **ade-qc** (docs/export_import_flow.md): TDR retrieveSnapshot -> BigQuery anvil_dataset.dataset_id
  -> Azul manifest (verbatim.pfb, poll) -> [shared tail].
- **tdr-qc** (docs/tdr_export_flow.md): TDR retrieveSnapshot -> TDR snapshot export (poll) -> signed
  manifest.json -> [shared tail].

The **shared tail** (``_create_import_and_qc``) is: Rawls createWorkspace (authorizationDomain: [])
-> Firecloud importJob -> poll to success -> QC (AW-vs-snapshot table comparison T1-T2 + auth-domain
verification). ade-qc uses Azul + an Azul token; tdr-qc uses Terra/ADC tokens throughout.

End-to-end execution requires real per-tier ADC credentials and a real TDR snapshot, and creates a
real (retained) workspace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests
from google.auth.credentials import Credentials

from . import __version__
from .auth import assert_tier_identity, bearer_token, credentials_for_tier
from .azul_auth import (
    MIN_TOKEN_SECONDS_FOR_RUN,
    AzulReindexingError,
    assert_azul_not_reindexing,
    expected_client_id,
    read_azul_token,
    validate_azul_token,
)
from .bigquery import check_bigquery_access, resolve_dataset_id, snapshot_data_volume
from .clients.azul import AzulClient
from .clients.firecloud import FirecloudClient, import_job_id
from .clients.rawls import RawlsClient, workspace_auth_domains
from .clients.tdr import (
    TdrClient,
    export_job_id,
    export_manifest_url,
    snapshot_bigquery_access,
    snapshot_consent_code,
    snapshot_name,
)
from .compare.profile import TDR_SYSTEM_TABLES
from .compare.qc import run_qc_comparison
from .config import SERVICE_URLS, ResolvedTier, load_config, resolve_tier
from .logging_setup import LOGGER_NAME, setup_logging, write_header
from .safety import (
    SIGNED_URL_ALLOWED_PREFIXES,
    ControlledAccessNotApprovedError,
    SafetyError,
    SignedUrl,
    assert_consent_permitted,
    is_open_access,
    tdr_export_allowed_prefixes,
    verify_pfb_handoff,
)
from .timing import log_stage_summary, stage_timer
from .workspace import ADE_NAME_INFIX, TDR_NAME_INFIX, workspace_name

logger = logging.getLogger(LOGGER_NAME)

# tdr-qc importJob shape (Terra import of a TDR snapshot export; see docs/tdr_export_flow.md).
TDR_IMPORT_FILETYPE = "tdrexport"
# ``tdrSyncPermissions=False`` for ALL data, incl. controlled-access. Confirmed correct + safe (2026-06-27)
# from a prod CONTROLLED-access capture (the GRU dGTEx flow used False) plus: the import applies the
# snapshot's auth domain to the workspace INDEPENDENTLY of this flag (verified), so False does not
# under-protect; and False is least-privilege -- a new workspace is owner-only and sharing stays gated by
# the auth domain, whereas True would sync the snapshot's reader policies (granting access we don't intend).
# See risk R8 and docs/tdr_export_flow.md.
TDR_SYNC_PERMISSIONS = False

# Rawls createWorkspace provisions a GCS bucket and is the slow call in the tail; run several-wide by
# the batch runner it can be slow enough that either the client read-times-out OR a proxy in front of
# Rawls returns a 5xx (observed: 502 Bad Gateway at ~120s) even while Rawls is healthy and the workspace
# was created server-side. Give the client headroom and -- on a timeout OR a 5xx -- confirm-or-adopt the
# workspace (see _create_workspace_resilient) rather than abandoning an orphaned (import-less) one. R4.
RAWLS_TIMEOUT = 300  # seconds
WORKSPACE_ADOPT_ATTEMPTS = 5  # GETs to confirm the workspace after a failed/uncertain create
WORKSPACE_ADOPT_POLL_SECONDS = 5.0  # between those GETs
WORKSPACE_ADOPT_GET_TIMEOUT = 30  # per-GET timeout, so a slow GET is not governed by RAWLS_TIMEOUT


def _http_status(exc: requests.exceptions.HTTPError) -> Optional[int]:
    """The HTTP status code carried by a requests ``HTTPError``, or ``None`` if unavailable."""
    return getattr(getattr(exc, "response", None), "status_code", None)


def _create_workspace_resilient(
    rawls: RawlsClient, namespace: str, name: str, *, description: str
) -> None:
    """Create the destination workspace, tolerating a slow/uncertain createWorkspace.

    Rawls createWorkspace can be slow under concurrent load, so the POST may either time out client-side
    or get a **5xx** from a proxy in front of Rawls (observed: ``502 Bad Gateway`` at ~120s under batch
    QC at ``--max-concurrent 5``) -- in both cases the workspace is often already created server-side, so
    aborting leaves an orphaned, import-less workspace. On a timeout/connection error OR a 5xx we
    therefore GET the workspace (retrying a not-yet-visible 404 or a transient 5xx/GET-timeout): if it
    exists with the empty ``authorizationDomain`` we requested we adopt it and continue into the import;
    if it cannot be confirmed after ``WORKSPACE_ADOPT_ATTEMPTS`` checks the create genuinely failed and we
    re-raise. A **4xx** (incl. ``409`` name-exists, ``403`` no-access) is a definitive error and stays
    fail-fast, as before.

    Safety: a workspace our POST just created must carry the empty ``authorizationDomain`` we requested
    (the import applies the real one later). If an adopted workspace already has a non-empty auth domain
    it is not the one we created -- refuse it (``SafetyError``) rather than import into an unexpected
    target.
    """
    try:
        rawls.create_workspace(namespace, name, description=description)
        return
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        create_exc = exc
        logger.warning(
            "createWorkspace for %s/%s did not return (%s); checking whether it was created "
            "server-side before giving up...",
            namespace,
            name,
            type(exc).__name__,
        )
    except requests.exceptions.HTTPError as exc:
        status = _http_status(exc)
        if status is None or status < 500:
            raise  # 4xx (incl. 409 name-exists / 403 no-access) or unknown -- definitive, fail-fast
        create_exc = exc
        logger.warning(
            "createWorkspace for %s/%s returned %s (gateway/server error -- likely a slow create behind "
            "a proxy); checking whether it was created server-side before giving up...",
            namespace,
            name,
            status,
        )
    # Confirm-or-adopt. A 404 means "not visible yet"; a transient 5xx or Timeout/ConnectionError on the
    # GET is the same contended condition that failed the create -- all just retry. Definitive 4xx propagates.
    for attempt in range(1, WORKSPACE_ADOPT_ATTEMPTS + 1):
        ws = None
        try:
            ws = rawls.get_workspace(
                namespace,
                name,
                fields="workspace.authorizationDomain",
                timeout=WORKSPACE_ADOPT_GET_TIMEOUT,
            )
        except requests.exceptions.HTTPError as get_exc:
            status = _http_status(get_exc)
            retryable = status == 404 or (status is not None and status >= 500)
            if not retryable:
                raise  # a definitive 4xx (403/...) -- do not mask it
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            pass  # same load that failed the create; treat as not-yet-visible and retry
        if ws is not None:
            domains = workspace_auth_domains(ws)
            if domains:
                raise SafetyError(
                    f"createWorkspace for {namespace}/{name} did not succeed and an existing workspace "
                    f"of that name has a non-empty auth domain {domains}; refusing to adopt it (a "
                    "just-created workspace must have none -- the import applies the auth domain)."
                )
            logger.info(
                "createWorkspace did not return cleanly but %s/%s exists server-side with an empty auth "
                "domain; adopting it and continuing to the import.",
                namespace,
                name,
            )
            return
        if attempt < WORKSPACE_ADOPT_ATTEMPTS:
            time.sleep(WORKSPACE_ADOPT_POLL_SECONDS)
    # Never confirmed the workspace -- the create genuinely failed (or Rawls stayed unreachable).
    raise RuntimeError(
        f"createWorkspace for {namespace}/{name} did not succeed and the workspace could not be "
        f"confirmed created after {WORKSPACE_ADOPT_ATTEMPTS} checks. Re-run to retry."
    ) from create_exc


@dataclass(frozen=True)
class _RunSetup:
    """Shared per-run setup: resolved tier, ADC credentials, a bearer-token provider, the log file."""

    tier: ResolvedTier
    creds: Credentials
    google_token: Callable[[], str]
    log_file: Path
    identity: str  # the Sam-resolved Terra user the active credentials operate as (identity guard)


def _setup_run(
    snapshot_id: str, tier_name: Optional[str], config_path: Path, log_dir: Path
) -> _RunSetup:
    """Load config, start logging, load ADC creds, and run the tier identity guard (shared by both flows)."""
    config = load_config(config_path)
    tier = resolve_tier(config, tier_name)

    _logger, log_file = setup_logging(log_dir, tier.name, snapshot_id)
    write_header(
        _logger,
        version=__version__,
        tier=tier.name,
        snapshot_id=snapshot_id,
        terra_billing_project=tier.terra_billing_project,
        billing_google_project_id=tier.billing_google_project_id,
        log_file=str(log_file),
    )

    creds = credentials_for_tier(tier)
    identity = assert_tier_identity(creds, tier)

    def google_token() -> str:
        return bearer_token(creds)

    return _RunSetup(
        tier=tier, creds=creds, google_token=google_token, log_file=log_file, identity=identity
    )


def _log_auth_verified(
    setup: _RunSetup, flow: str, snapshot_id: str, snapshot_name: str, consent_code: Optional[str]
) -> dict:
    """Log the ``--verify-auth`` confirmation and return its result dict (no export/import performed).

    Reached only after the full auth/authorization preflight has passed: the identity guard, the
    snapshot read (so the operator is entitled to this snapshot), the consent gate, the BigQuery-access
    check (``check_bigquery_access``), and -- for ade-qc -- the Azul-token check. A clear, self-contained
    "you are operating as X and may run this" line for an operator who switches identities/credentials
    between tiers and data programs.
    """
    access = "open-access" if is_open_access(consent_code) else "controlled-access (authorized)"
    logger.info(
        "✓ AUTH VERIFIED — %s, tier %s: operating as %s; snapshot %r (%s) is readable and "
        "%s (consentCode=%r). No export/import performed (--verify-auth).",
        flow,
        setup.tier.name,
        setup.identity,
        snapshot_name,
        snapshot_id,
        access,
        consent_code,
    )
    return {
        "verify_auth": True,
        "identity": setup.identity,
        "tier": setup.tier.name,
        "snapshot_name": snapshot_name,
        "consent_code": consent_code,
        "open_access": is_open_access(consent_code),
        "log_file": str(setup.log_file),
    }


def _create_import_and_qc(
    *,
    setup: _RunSetup,
    snapshot_id: str,
    name: str,
    bq_project: str,
    bq_dataset: str,
    signed_url: SignedUrl,
    filetype: str,
    import_options: Optional[dict],
    allowed_prefixes: tuple[str, ...],
    infix: str,
    description: str,
    durations: dict[str, float],
) -> dict:
    """Shared tail for both flows: create the workspace, hand off the signed URL to the import, wait,
    then QC the workspace against the snapshot.

    Identical for ade-qc and tdr-qc -- only the signed-URL source, importJob ``filetype``/``options``,
    provenance ``allowed_prefixes``, workspace ``infix``, and ``description`` differ (passed in).
    """
    tier = setup.tier
    google_token = setup.google_token

    # Rawls: create the destination workspace (empty auth domain; the import applies it).
    ws_name = workspace_name(tier.email, name, infix=infix)
    rawls = RawlsClient(tier.url("rawls"), google_token, timeout=RAWLS_TIMEOUT)
    logger.info("Creating workspace %s/%s", tier.terra_billing_project, ws_name)
    try:
        with stage_timer("workspace_create", durations):
            _create_workspace_resilient(
                rawls, tier.terra_billing_project, ws_name, description=description
            )
        # Stable, greppable marker for the created workspace, logged BEFORE the import so it is present
        # even if the import later fails (the retained workspace still exists). The WDL parses it into the
        # workspace_namespace/workspace_name data-table columns -- for a later `compare`, cross-operator
        # discovery/access of another operator's run, or (bulk) cleanup. namespace + name are both from
        # [A-Za-z0-9_-] (no spaces/slashes), so the single '/' splits them cleanly.
        logger.info("QC workspace: %s/%s", tier.terra_billing_project, ws_name)

        # Firecloud: import and wait for the job to succeed. Dev Terra can be slow (createWorkspace
        # alone took ~20s); give the import submit/polls headroom.
        firecloud = FirecloudClient(tier.url("firecloud"), google_token, timeout=180)
        # Verify the signed URL is delivered only to this tier's import service, and is from the
        # expected source, before handing it over -- defense against a controlled signed URL reaching a
        # wrong/dev destination (a Federal DMI). See safety.verify_pfb_handoff.
        verify_pfb_handoff(
            signed_url,
            tier_name=tier.name,
            expected_firecloud_host=urlsplit(SERVICE_URLS[tier.name]["firecloud"]).netloc,
            destination_host=urlsplit(firecloud.base_url).netloc,
            allowed_prefixes=allowed_prefixes,
        )
        with stage_timer("import", durations):
            submit = firecloud.submit_import_job(
                tier.terra_billing_project,
                ws_name,
                signed_url,
                filetype=filetype,
                options=import_options,
            )
            job_id = import_job_id(submit)
            logger.info("Import job submitted: %s", job_id)
            firecloud.wait_for_import(tier.terra_billing_project, ws_name, job_id)

        logger.info("Export/import complete. Workspace: %s/%s", tier.terra_billing_project, ws_name)

        # QC: compare the AW data tables to the TDR snapshot (T1-T2) and verify the auth domain. (QC is
        # not a timed stage -- quick, not a user-experienced delay, like the `compare` command.)
        qc = run_qc_comparison(
            creds=setup.creds,
            tier=tier,
            snapshot_id=snapshot_id,
            bq_project=bq_project,
            bq_dataset=bq_dataset,
            namespace=tier.terra_billing_project,
            workspace_name=ws_name,
            token_provider=google_token,
        )
        logger.info("QC report:\n%s", qc.render())
        if not qc.passed:
            logger.warning(
                "QC FAILED for %s/%s -- see the report above.",
                tier.terra_billing_project,
                ws_name,
            )
        return {
            "workspace_namespace": tier.terra_billing_project,
            "workspace_name": ws_name,
            "snapshot_name": name,
            "qc_passed": qc.passed,
            "durations": durations,
            "log_file": str(setup.log_file),
        }
    finally:
        # End-of-run summary of the user-experienced stage delays (minutes), appended to the QC report
        # and parsed into the WDL duration outputs. In a `finally` so it surfaces even when the import
        # fails/times out -- the partial durations (e.g. the ~2h import-until-timeout) are exactly what
        # an operator needs to triage a failure.
        log_stage_summary(durations)


def run_ade_qc(
    snapshot_id: str,
    tier_name: Optional[str],
    config_path: Path,
    log_dir: Path = Path("logs"),
    dry_run: bool = False,
    catalog: Optional[str] = None,
    allow_controlled_access: bool = False,
    azul_token_file: Optional[str] = None,
    verify_auth: bool = False,
) -> dict:
    """Run the ADE/Azul export/import flow for one TDR snapshot. Returns the created workspace + log path.

    ``verify_auth`` is an auth/authorization preflight: it runs the identity guard, validates the Azul
    token, reads the snapshot, checks the consent gate, and verifies BigQuery access
    (``check_bigquery_access``), then logs a confirmation and returns -- **before** any export, signed
    URL, workspace, or import. ``dry_run`` (weaker) stops after Azul returns the signed URL (steps 1-3)
    -- no workspace is created and no import is run.

    ``allow_controlled_access`` opts in to running on non-open-access snapshots (default off = fail
    closed, open-access only; see ``safety.assert_consent_permitted``). ``azul_token_file`` overrides
    the tier's configured Azul token path (used by the WDL to pass a localized token file).
    """
    setup = _setup_run(snapshot_id, tier_name, config_path, log_dir)
    tier = setup.tier
    google_token = setup.google_token

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

    # BigQuery reachability preflight: confirm jobs can run in billing_google_project_id (a trivial
    # SELECT 1) so a wrong project fails cleanly here -- including under --verify-auth -- rather than as
    # a mid-run traceback. See check_bigquery_access.
    check_bigquery_access(setup.creds, tier.billing_google_project_id)

    # Auth/authorization preflight: identity + Azul token + snapshot read + consent + BigQuery all
    # verified above. Stop here -- before any export/signed URL/workspace/import.
    if verify_auth:
        return _log_auth_verified(setup, "ade-qc", snapshot_id, name, consent_code)

    bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
    logger.info("Snapshot '%s' -> BigQuery %s.%s", name, bq_project, bq_dataset)

    # Data-volume metrics from the snapshot (logged now, before the export/import, so they are
    # available even if a later step fails). Excludes system + empty tables; see snapshot_data_volume.
    volume = snapshot_data_volume(
        setup.creds,
        bq_project,
        bq_dataset,
        tier.billing_google_project_id,
        ignore_tables=TDR_SYSTEM_TABLES,
    )

    # 2. BigQuery: resolve the Azul dataset_id from the snapshot (system of record).
    dataset_id = resolve_dataset_id(
        setup.creds, bq_project, bq_dataset, tier.billing_google_project_id
    )

    # Azul reindexing preflight (ade-qc only). During a UCSC/Azul (re)index the manifest API still
    # returns 200 but the manifest is EMPTY/incomplete (Azul is unaware of the in-progress index), which
    # makes QC results unreliable and surfaces later as a confusing import "not found or empty" failure.
    # Fail fast on a detected (re)index -- AzulReindexingError -> CLI exit 2 (retryable). A health-endpoint
    # error is NOT fatal here: a transient blip must not block a run, and a genuine reindex is still caught
    # by the import backstop -- so we log and proceed. Covers dry runs too (a dry run also builds a
    # manifest). See azul_auth.assert_azul_not_reindexing.
    try:
        assert_azul_not_reindexing(tier.url("azul"))
    except AzulReindexingError as exc:
        logger.error("%s", exc)
        raise
    except (requests.RequestException, ValueError) as exc:  # network blip / malformed health response
        logger.warning("Could not check Azul indexing status (%s); proceeding.", exc)

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
        log_stage_summary(durations)  # export stage only on a dry run
        return {
            "dry_run": True,
            "snapshot_name": name,
            "bigquery": f"{bq_project}.{bq_dataset}",
            "dataset_id": dataset_id,
            "signed_url_location": location,
            "durations": durations,
            "log_file": str(setup.log_file),
            **asdict(volume),
        }

    return {
        **_create_import_and_qc(
            setup=setup,
            snapshot_id=snapshot_id,
            name=name,
            bq_project=bq_project,
            bq_dataset=bq_dataset,
            signed_url=signed_url,
            filetype="pfb",
            import_options=None,
            allowed_prefixes=SIGNED_URL_ALLOWED_PREFIXES.get(tier.name, ()),
            infix=ADE_NAME_INFIX,
            description=f"AnVIL Data Release QC export of {name}",
            durations=durations,
        ),
        **asdict(volume),
    }


def run_tdr_qc(
    snapshot_id: str,
    tier_name: Optional[str],
    config_path: Path,
    log_dir: Path = Path("logs"),
    dry_run: bool = False,
    allow_controlled_access: bool = False,
    verify_auth: bool = False,
) -> dict:
    """Run the TDR direct-export + QC flow for one TDR snapshot. Returns the created workspace + log path.

    Exports the snapshot DIRECTLY from TDR (no ADE/Azul, no Azul token): TDR snapshot export -> signed
    manifest.json -> Terra import (filetype ``tdrexport``) -> QC. Uses Terra/ADC tokens throughout. For
    very large datasets that fail to export via ADE/Azul.

    ``verify_auth`` is an auth/authorization preflight: it runs the identity guard, reads the snapshot,
    checks the consent gate, and verifies BigQuery access (``check_bigquery_access``), then logs a
    confirmation and returns -- **before** any export, signed URL, workspace, or import. ``dry_run``
    (weaker) stops after the export manifest is obtained -- no
    workspace is created and no import is run. ``allow_controlled_access`` opts in to running on
    non-open-access snapshots (default off = fail closed, open-access only); no Azul token is needed
    (the operator's own Terra credentials must grant access to the controlled data). See
    ``safety.assert_consent_permitted``.
    """
    setup = _setup_run(snapshot_id, tier_name, config_path, log_dir)
    tier = setup.tier
    google_token = setup.google_token

    # 1. TDR: retrieve the snapshot (name, consentCode, BigQuery access info).
    tdr = TdrClient(tier.url("tdr"), google_token)
    snapshot = tdr.retrieve_snapshot(snapshot_id)
    name = snapshot_name(snapshot)

    # SAFETY GATE (layer 1): only explicitly open-access snapshots proceed UNLESS controlled-access was
    # explicitly authorized for this run (allow_controlled_access). The TDR export signed URL grants
    # direct access to the snapshot's data; mishandling it (e.g. a prod URL into the insecure dev tier)
    # is a Federal DMI. Controlled-access is security-team approved, gated behind the explicit opt-in;
    # layers 2-4 apply regardless. Unlike ade-qc there is no Azul token -- the operator's own Terra
    # credentials must already grant access to the controlled data (TDR /export 403s otherwise), and the
    # import applies the snapshot's auth domain to the workspace independently of tdrSyncPermissions
    # (verified post-import by the QC auth-domain check). Runs before any export/signed URL is generated.
    consent_code = snapshot_consent_code(snapshot)
    try:
        assert_consent_permitted(
            snapshot_id, consent_code, allow_controlled_access=allow_controlled_access
        )
    except ControlledAccessNotApprovedError as exc:
        logger.error("%s", exc)
        raise
    if is_open_access(consent_code):
        logger.info("Consent gate OK: snapshot is open-access (consentCode=%r).", consent_code)
    else:
        logger.warning(
            "CONTROLLED-ACCESS run AUTHORIZED (allow_controlled_access=True): snapshot %s, "
            "consentCode=%r, tier %s. The TDR export signed URL grants direct controlled-data access; "
            "hand-off verification and bearer-pinning still apply. The import applies the snapshot's "
            "auth domain (verified by the QC auth-domain check).",
            snapshot_id,
            consent_code,
            tier.name,
        )

    # BigQuery reachability preflight: confirm jobs can run in billing_google_project_id (a trivial
    # SELECT 1) so a wrong project fails cleanly here -- including under --verify-auth -- rather than as
    # a mid-run traceback. See check_bigquery_access.
    check_bigquery_access(setup.creds, tier.billing_google_project_id)

    # Auth/authorization preflight: identity + snapshot read + consent + BigQuery all verified above.
    # Stop here -- before any export/signed URL/workspace/import.
    if verify_auth:
        return _log_auth_verified(setup, "tdr-qc", snapshot_id, name, consent_code)

    bq_project, bq_dataset = snapshot_bigquery_access(snapshot)
    logger.info("Snapshot '%s' -> BigQuery %s.%s", name, bq_project, bq_dataset)

    # Data-volume metrics from the snapshot (logged now, before the export/import, so they are
    # available even if a later step fails). Excludes system + empty tables; see snapshot_data_volume.
    volume = snapshot_data_volume(
        setup.creds,
        bq_project,
        bq_dataset,
        tier.billing_google_project_id,
        ignore_tables=TDR_SYSTEM_TABLES,
    )

    # 2-3. TDR: export the snapshot (async) and obtain the signed manifest.json URL.
    durations: dict[str, float] = {}
    with stage_timer("tdr_export", durations):
        job = tdr.export_snapshot(snapshot_id)
        job_id = export_job_id(job)
        logger.info("TDR export job submitted: %s", job_id)
        tdr.wait_for_export_job(job_id)
        signed_url = export_manifest_url(tdr.get_job_result(job_id))

    if dry_run:
        location = signed_url.location  # scheme+host+path only; the signed query stays contained
        logger.info(
            "Dry run: export side validated (TDR snapshot export). Signed manifest URL at %s. "
            "Skipping workspace creation and import.",
            location,
        )
        log_stage_summary(durations)  # export stage only on a dry run
        return {
            "dry_run": True,
            "snapshot_name": name,
            "bigquery": f"{bq_project}.{bq_dataset}",
            "signed_url_location": location,
            "durations": durations,
            "log_file": str(setup.log_file),
            **asdict(volume),
        }

    return {
        **_create_import_and_qc(
            setup=setup,
            snapshot_id=snapshot_id,
            name=name,
            bq_project=bq_project,
            bq_dataset=bq_dataset,
            signed_url=signed_url,
            filetype=TDR_IMPORT_FILETYPE,
            import_options={"tdrSyncPermissions": TDR_SYNC_PERMISSIONS},
            allowed_prefixes=tdr_export_allowed_prefixes(bq_project),
            infix=TDR_NAME_INFIX,
            description=f"AnVIL Data Release TDR-export QC of {name}",
            durations=durations,
        ),
        **asdict(volume),
    }
