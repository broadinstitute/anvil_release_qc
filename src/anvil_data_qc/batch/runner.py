"""The qc_batch_runner core: pre-flight, a concurrency-capped control loop, and outcome classification.

Drives the Terra submissions API to run one QC pipeline (``ade_qc``/``tdr_qc``) over a list of
``snapshot_id``s (data-table rows), holding at most ``max_concurrent`` submissions in flight to protect
shared production. The per-dataset QC verdict lives in the data-table columns (``<prefix>_qc_passed`` /
``<prefix>_qc_exit_code``); the runner reads those to classify outcomes and to skip rows that already
passed. Design + rationale: docs/plan (co-designed 2026-08).

Concurrency policy is a swappable ``can_launch`` seam: **v1 = batch-drain** (launch a wave of ≤cap, wait
for it to fully drain, then the next); **v2 (future) = sliding window** (top up whenever in-flight < cap).
Only the predicate changes -- the loop, accounting, token-pacing, and classification are shared.

Python 3.10-compatible (no 3.11+ syntax) so it can also run in Terra Cloud environments.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import requests
from google.auth.credentials import Credentials

from ..azul_auth import (
    MIN_TOKEN_SECONDS_FOR_RUN,
    AzulReindexingError,
    AzulTokenError,
    assert_azul_not_reindexing,
    expected_client_id,
    token_from_file_text,
    validate_azul_token,
)
from ..config import ResolvedTier
from ..logging_setup import LOGGER_NAME
from .gcs import read_gcs_text
from .terra import (
    TerraWorkspaceClient,
    is_submission_terminal,
    submission_id,
    workflow_statuses,
    WORKFLOW_ABORTED,
    WORKFLOW_FAILED,
)

logger = logging.getLogger(LOGGER_NAME)

# Pipeline -> workspace method-config name (confirmed from the anvil-data-qc-shared workspace).
PIPELINE_METHOD_CONFIG = {
    "ade_qc": "anvil-data-qc-ade-qc",
    "tdr_qc": "anvil-data-qc-tdr-qc",
}
# Pipeline -> data-table output-column prefix (ade_qc -> ade_qc_passed / ade_qc_exit_code).
PIPELINE_COLUMN_PREFIX = {"ade_qc": "ade", "tdr_qc": "tdr"}
# Pipelines that need a valid Azul token (controlled-access data). tdr_qc never does.
PIPELINES_NEEDING_AZUL_TOKEN = frozenset({"ade_qc"})

# Workspace Data key holding the gs:// path to the Azul token file.
AZUL_TOKEN_ATTRIBUTE = "anvil_data_qc_azul_token"

# Data-table column that marks a row to EXCLUDE from batch runs (e.g. datasets known to exceed the Terra
# import ceiling). Empty/absent = run normally; any non-empty value = skip, and the value IS the
# human-readable reason (e.g. "too_large_import_ceiling", "xfail:R4"), surfaced in the log + summary and
# available for the release QC report. Shared across ade_qc/tdr_qc (both import, so both honor it).
# Override with --run-skipped.
SKIP_ATTRIBUTE = "qc_skip"

# Token TTL gate = the pipeline's own requirement + a launch->manifest lag buffer, so the launched
# workflow still finds >= its own minimum when it reaches the Azul step. Derived from the pipeline
# constant (MIN_TOKEN_SECONDS_FOR_RUN) so the two can't drift.
LAG_BUFFER_SECONDS = 300
DEFAULT_TOKEN_MIN_SECONDS = MIN_TOKEN_SECONDS_FOR_RUN + LAG_BUFFER_SECONDS  # 2100 (35 min)

DEFAULT_MAX_CONCURRENT = 5
MAX_CONCURRENT_CEILING = 10  # fat-finger guard: the runner is meant to keep shared-prod load modest
DEFAULT_SUBMISSION_POLL_SECONDS = 30.0
DEFAULT_TOKEN_POLL_SECONDS = 60.0
DEFAULT_TOKEN_MAX_WAIT_SECONDS = 7200.0  # 2 h


class RunnerFatalError(RuntimeError):
    """A runner-level condition that stops the whole batch (exit 2): bad input/config, no/expired token
    past the wait limit, etc. Distinct from a single dataset's QC/infra failure (continue-on-failure)."""


class Outcome(str, Enum):
    PASSED = "passed"
    QC_FAILED = "qc_failed"  # exit 1
    SAFETY_ABORT = (
        "safety_abort"  # exit 2 (consent / token / BigQuery preflight) -- often retryable
    )
    INFRA_FAILED = "infra_failed"  # Cromwell workflow Failed (no outputs) -- retry candidate
    ABORTED = "aborted"
    SKIPPED = "skipped"  # already passed (idempotency)
    EXCLUDED = "excluded"  # marked to skip via the qc_skip column (not a failure); reason is logged
    LAUNCH_ERROR = "launch_error"  # create_submission raised
    INCOMPLETE = "incomplete"  # workflow Succeeded but no exit_code/pass in the row (unexpected)
    WOULD_RUN = "would_run"  # dry-run only

    @property
    def is_failure(self) -> bool:
        return self in {
            Outcome.QC_FAILED,
            Outcome.SAFETY_ABORT,
            Outcome.INFRA_FAILED,
            Outcome.ABORTED,
            Outcome.LAUNCH_ERROR,
            Outcome.INCOMPLETE,
        }


# --- concurrency policies (the swappable seam) ---


def batch_drain_policy(in_flight: int, cap: int) -> bool:
    """v1: only start a new wave once the previous one has fully drained (in-flight back to 0)."""
    return in_flight == 0


def sliding_window_policy(in_flight: int, cap: int) -> bool:
    """v2 (future): keep the pipe full -- launch whenever there's a free slot."""
    return in_flight < cap


@dataclass
class BatchResult:
    """Per-run outcome tally + per-snapshot outcomes, and the derived process exit code."""

    outcomes: dict = field(default_factory=dict)  # snapshot_id -> Outcome
    reasons: dict = field(default_factory=dict)  # snapshot_id -> reason string (EXCLUDED rows)
    dry_run: bool = False

    def counts(self) -> dict:
        c: dict = {}
        for o in self.outcomes.values():
            c[o] = c.get(o, 0) + 1
        return c

    @property
    def exit_code(self) -> int:
        if self.dry_run:
            return 0
        return 1 if any(o.is_failure for o in self.outcomes.values()) else 0

    def render(self) -> str:
        counts = self.counts()
        order = list(Outcome)
        head = "DRY RUN — would process" if self.dry_run else "Batch complete"
        lines = [
            f"{head}: {len(self.outcomes)} snapshot(s) — "
            + ", ".join(f"{o.value}={counts[o]}" for o in order if counts.get(o))
        ]
        for sid, o in self.outcomes.items():
            reason = self.reasons.get(sid)
            lines.append(f"  {o.value:<12} {sid}" + (f"  ({reason})" if reason else ""))
        return "\n".join(lines)


@dataclass
class BatchParams:
    pipeline: str  # "ade_qc" | "tdr_qc"
    entity_type: str  # the data table name, e.g. "anvil15_qc"
    snapshot_ids: list  # ordered list of snapshot_ids (== entity ids)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    dry_run: bool = False
    force: bool = False  # re-run rows that already passed
    run_skipped: bool = False  # also run rows marked to skip via the qc_skip column
    on_stale_token: str = "auto"  # "auto" (TTY) | "prompt" | "wait"
    token_min_seconds: int = DEFAULT_TOKEN_MIN_SECONDS
    token_poll_seconds: float = DEFAULT_TOKEN_POLL_SECONDS
    token_max_wait_seconds: float = DEFAULT_TOKEN_MAX_WAIT_SECONDS
    submission_poll_seconds: float = DEFAULT_SUBMISSION_POLL_SECONDS


# --- helpers ---


def _passed_attr(pipeline: str) -> str:
    return f"{PIPELINE_COLUMN_PREFIX[pipeline]}_qc_passed"


def _exit_code_attr(pipeline: str) -> str:
    return f"{PIPELINE_COLUMN_PREFIX[pipeline]}_qc_exit_code"


def resolve_method_config(client: TerraWorkspaceClient, pipeline: str, entity_type: str) -> tuple:
    """Return ``(namespace, name)`` for the pipeline's method config; validate it exists + matches the table."""
    name = PIPELINE_METHOD_CONFIG[pipeline]
    cfg = client.find_method_config(name)
    if cfg is None:
        raise RunnerFatalError(
            f"Method config {name!r} not found in workspace {client.namespace}/{client.name}."
        )
    root = cfg.get("rootEntityType")
    if root != entity_type:
        raise RunnerFatalError(
            f"Method config {name!r} rootEntityType is {root!r}, but the requested data table is "
            f"{entity_type!r}. Point the runner at the table the config is bound to, or reconfigure it."
        )
    return cfg.get("namespace"), name


def read_row(client: TerraWorkspaceClient, entity_type: str, snapshot_id: str) -> Optional[dict]:
    """Return a row's attributes dict, or ``None`` if the entity does not exist (HTTP 404)."""
    try:
        return client.get_entity(entity_type, snapshot_id).get("attributes") or {}
    except requests.HTTPError as exc:
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 404:
            return None
        raise


def _row_already_passed(attrs: dict, pipeline: str) -> bool:
    return attrs.get(_passed_attr(pipeline)) is True or attrs.get(_exit_code_attr(pipeline)) == 0


def _skip_reason(attrs: dict) -> str:
    """The row's ``qc_skip`` disposition, or ``""`` when unset (empty/absent ⇒ run normally).

    Any non-empty value marks the row to be EXCLUDED (not submitted); the value itself is the
    human-readable reason (e.g. ``too_large_import_ceiling``, ``xfail:R4``).
    """
    raw = attrs.get(SKIP_ATTRIBUTE)
    return str(raw).strip() if raw is not None else ""


def check_azul_token(
    client: TerraWorkspaceClient, creds: Credentials, tier: ResolvedTier, min_seconds: int
):
    """Return ``(ok, detail)`` for the Workspace-Data Azul token. Never logs the token itself.

    ``ok`` True means the token exists and passes ``validate_azul_token`` (azp + account + >= min_seconds).
    ``detail`` is a human string (validated metadata on success, or the redaction-safe failure reason).
    """
    attrs = client.get_workspace_attributes()
    gs_uri = attrs.get(AZUL_TOKEN_ATTRIBUTE)
    if not gs_uri:
        return False, f"Workspace Data key {AZUL_TOKEN_ATTRIBUTE!r} is not set."
    # The token file carries a #-comment header above the token line (same format azul-login writes);
    # extract the token line exactly as the local reader does, so the two parse it identically.
    token = token_from_file_text(read_gcs_text(creds, gs_uri))  # SECRET -- never log `token`
    if not token:
        return False, f"Token file {gs_uri} has no token line (empty or only comments)."
    try:
        info = validate_azul_token(
            token,
            expected_azp=expected_client_id(tier),
            allowed_emails=tier.authorized_emails,
            min_remaining_seconds=min_seconds,
        )
    except AzulTokenError as exc:
        return False, str(exc)  # message carries azp/account/expiry, not the token value
    return True, f"account {info.get('email')}, expires in {info.get('expires_in')}s"


def ensure_token_available(
    client: TerraWorkspaceClient,
    creds: Credentials,
    tier: ResolvedTier,
    params: BatchParams,
    *,
    interactive: bool,
) -> None:
    """Block until a valid Azul token with >= ``token_min_seconds`` TTL is available (ade_qc only).

    Interactive: prompt the operator to refresh (azul-login + upload) and re-check on Enter. Non-interactive:
    poll every ``token_poll_seconds`` up to ``token_max_wait_seconds``, then raise ``RunnerFatalError``.
    Re-reads the token each check (no caching), so a mid-run refresh is picked up.
    """
    waited = 0.0
    while True:
        ok, detail = check_azul_token(client, creds, tier, params.token_min_seconds)
        if ok:
            logger.info("Azul token OK: %s", detail)
            return
        need_min = params.token_min_seconds
        if interactive:
            logger.warning("Azul token not usable (need >= %ds TTL): %s", need_min, detail)
            input(
                f"  → Refresh the Azul token (azul-login + upload to Workspace Data '{AZUL_TOKEN_ATTRIBUTE}'), "
                "then press Enter to re-check... "
            )
            continue
        if waited >= params.token_max_wait_seconds:
            raise RunnerFatalError(
                f"No usable Azul token after waiting {int(waited)}s (need >= {need_min}s TTL): {detail}"
            )
        logger.warning(
            "Azul token not usable (need >= %ds TTL): %s — re-checking in %ds (waited %ds/%ds).",
            need_min,
            detail,
            int(params.token_poll_seconds),
            int(waited),
            int(params.token_max_wait_seconds),
        )
        time.sleep(params.token_poll_seconds)
        waited += params.token_poll_seconds


def classify(
    client: TerraWorkspaceClient,
    entity_type: str,
    snapshot_id: str,
    pipeline: str,
    submission: dict,
) -> Outcome:
    """Map a terminal submission + the resulting data-table row to a per-dataset outcome."""
    statuses = workflow_statuses(submission)
    if WORKFLOW_FAILED in statuses:
        return Outcome.INFRA_FAILED  # workflow failed at the Cromwell level -> no QC outputs
    if WORKFLOW_ABORTED in statuses:
        return Outcome.ABORTED
    attrs = read_row(client, entity_type, snapshot_id) or {}
    passed = attrs.get(_passed_attr(pipeline))
    exit_code = attrs.get(_exit_code_attr(pipeline))
    if passed is True or exit_code == 0:
        return Outcome.PASSED
    if exit_code == 1:
        return Outcome.QC_FAILED
    if exit_code == 2:
        return Outcome.SAFETY_ABORT
    return Outcome.INCOMPLETE  # Succeeded but no verdict recorded -- unexpected; surface it


def _preflight(
    client: TerraWorkspaceClient,
    creds: Credentials,
    tier: ResolvedTier,
    params: BatchParams,
) -> tuple:
    """Validate config + inputs; return ``(method_ns, method_name, run_list, skip_list, excluded)``.

    ``excluded`` is a list of ``(snapshot_id, reason)`` for rows marked via the ``qc_skip`` column
    (unless ``--run-skipped``). Fail-closed. The ``qc_skip`` marker is checked before the already-passed
    idempotency cull, and independently of ``--force``.
    """
    method_ns, method_name = resolve_method_config(client, params.pipeline, params.entity_type)
    logger.info(
        "Method config resolved: %s/%s (rootEntityType=%s).",
        method_ns,
        method_name,
        params.entity_type,
    )

    if not params.snapshot_ids:
        raise RunnerFatalError("Empty snapshot_id list — nothing to do.")

    missing, run_list, skip_list, excluded = [], [], [], []
    for sid in params.snapshot_ids:
        attrs = read_row(client, params.entity_type, sid)
        if attrs is None:
            missing.append(sid)
            continue
        reason = _skip_reason(attrs)
        if reason and not params.run_skipped:
            excluded.append((sid, reason))
        elif not params.force and _row_already_passed(attrs, params.pipeline):
            skip_list.append(sid)
        else:
            run_list.append(sid)
    if missing:
        raise RunnerFatalError(
            f"{len(missing)} snapshot_id(s) are not rows in table {params.entity_type!r} "
            f"(deleted/replaced?): {missing[:10]}{' …' if len(missing) > 10 else ''}"
        )
    for sid, reason in excluded:
        logger.info("Excluding %s (qc_skip=%r).", sid, reason)
    logger.info(
        "Pre-flight OK: %d total, %d excluded (qc_skip%s), %d already passed (skip%s), %d to run.",
        len(params.snapshot_ids),
        len(excluded),
        " — use --run-skipped to run" if excluded else "",
        len(skip_list),
        " — use --force to re-run" if skip_list and not params.force else "",
        len(run_list),
    )
    return method_ns, method_name, run_list, skip_list, excluded


def _resolve_interactive(on_stale_token: str) -> bool:
    if on_stale_token == "prompt":
        return True
    if on_stale_token == "wait":
        return False
    return sys.stdin.isatty()  # "auto"


def run_verify_auth(
    *,
    client: TerraWorkspaceClient,
    creds: Credentials,
    tier: ResolvedTier,
    pipeline: str,
    entity_type: str,
    identity: str,
    token_min_seconds: int = DEFAULT_TOKEN_MIN_SECONDS,
) -> str:
    """Side-effect-free auth/access preflight (no submissions). Mirrors the pipelines' ``--verify-auth``.

    Assumes the caller has already run the identity guard (``assert_tier_identity``) and passes the
    resolved Terra user as ``identity``. Then confirms, without side effects: the shared workspace + the
    pipeline's method config are reachable and the config's ``rootEntityType`` matches ``entity_type``;
    and -- for ``ade_qc`` -- that a usable Azul token is present (``check_azul_token``). Returns a one-line
    human summary; raises ``RunnerFatalError`` (or ``IdentityMismatchError`` upstream) on any problem.
    """
    method_ns, method_name = resolve_method_config(client, pipeline, entity_type)
    if pipeline in PIPELINES_NEEDING_AZUL_TOKEN:
        ok, detail = check_azul_token(client, creds, tier, token_min_seconds)
        if not ok:
            raise RunnerFatalError(f"Azul token not usable for {pipeline}: {detail}")
        token_line = f"Azul token OK ({detail})"
        # Azul reindexing preflight (ade_qc only): refuse to green-light a batch while Azul is
        # (re)indexing -- manifests would be empty/incomplete (see azul_auth.assert_azul_not_reindexing).
        # This complements the pipeline's own per-run check, which catches a reindex that starts
        # mid-batch. Fail-open on a health-endpoint blip: warn, don't block.
        try:
            assert_azul_not_reindexing(tier.url("azul"))
            reindex_line = "Azul indexing idle"
        except AzulReindexingError as exc:
            raise RunnerFatalError(str(exc)) from exc
        except (requests.RequestException, ValueError) as exc:  # blip / malformed health response
            logger.warning("Could not check Azul indexing status: %s; proceeding.", exc)
            reindex_line = f"Azul indexing status unknown ({exc})"
    else:
        token_line = "Azul token not required (tdr_qc)"
        reindex_line = "Azul indexing check not applicable (tdr_qc)"
    summary = (
        f"AUTH VERIFIED — tier {tier.name}: operating as {identity}; "
        f"workspace {client.namespace}/{client.name} reachable; "
        f"method config {method_ns}/{method_name} (rootEntityType={entity_type}); "
        f"{token_line}; {reindex_line}"
    )
    logger.info("✓ %s", summary)
    return summary


def run_batch(
    *,
    client: TerraWorkspaceClient,
    creds: Credentials,
    tier: ResolvedTier,
    params: BatchParams,
    launch_policy: Callable[[int, int], bool] = batch_drain_policy,
) -> BatchResult:
    """Run the batch: pre-flight, then a concurrency-capped launch/poll loop; returns a ``BatchResult``.

    ``launch_policy`` is the swappable seam (default v1 batch-drain; pass ``sliding_window_policy`` for v2).
    Continue-on-failure: a single dataset's failure is recorded and the batch proceeds; only
    ``RunnerFatalError`` stops it. In dry-run, no submissions are created — just the effective plan.
    """
    if not (1 <= params.max_concurrent <= MAX_CONCURRENT_CEILING):
        raise RunnerFatalError(
            f"max_concurrent={params.max_concurrent} out of range [1, {MAX_CONCURRENT_CEILING}]."
        )
    needs_token = params.pipeline in PIPELINES_NEEDING_AZUL_TOKEN
    interactive = _resolve_interactive(params.on_stale_token)

    method_ns, method_name, run_list, skip_list, excluded = _preflight(client, creds, tier, params)
    result = BatchResult(dry_run=params.dry_run)
    for sid in skip_list:
        result.outcomes[sid] = Outcome.SKIPPED
    for sid, reason in excluded:
        result.outcomes[sid] = Outcome.EXCLUDED
        result.reasons[sid] = reason

    if params.dry_run:
        if needs_token:
            ok, detail = check_azul_token(client, creds, tier, params.token_min_seconds)
            logger.info("Azul token check (dry-run): %s — %s", "OK" if ok else "NOT USABLE", detail)
        for sid in run_list:
            result.outcomes[sid] = Outcome.WOULD_RUN
        logger.info(
            "DRY RUN: would submit %d %s workflow(s) at max_concurrent=%d; "
            "%d skipped (already passed), %d excluded (qc_skip).",
            len(run_list),
            params.pipeline,
            params.max_concurrent,
            len(skip_list),
            len(excluded),
        )
        return result

    pending = list(run_list)
    in_flight: dict = {}  # submission_id -> snapshot_id
    while pending or in_flight:
        # LAUNCH phase (gated by the concurrency policy + the Azul token when required).
        if pending and launch_policy(len(in_flight), params.max_concurrent):
            if needs_token:
                ensure_token_available(client, creds, tier, params, interactive=interactive)
            slots = params.max_concurrent - len(in_flight)
            for _ in range(slots):
                if not pending:
                    break
                sid = pending.pop(0)
                try:
                    submit = client.create_submission(
                        method_config_namespace=method_ns,
                        method_config_name=method_name,
                        entity_type=params.entity_type,
                        entity_name=sid,
                        use_call_cache=False,
                        user_comment=f"qc_batch_runner {params.pipeline} {sid}",
                    )
                    sub_id = submission_id(submit)
                    in_flight[sub_id] = sid
                    logger.info(
                        "Launched %s for %s → submission %s (in-flight %d/%d).",
                        params.pipeline,
                        sid,
                        sub_id,
                        len(in_flight),
                        params.max_concurrent,
                    )
                except (
                    Exception
                ) as exc:  # continue-on-failure: one launch error must not stop the batch
                    result.outcomes[sid] = Outcome.LAUNCH_ERROR
                    logger.error("Launch FAILED for %s: %s", sid, exc)
        # POLL phase.
        if in_flight:
            time.sleep(params.submission_poll_seconds)
            for sub_id in list(in_flight):
                sid = in_flight[sub_id]
                try:
                    submission = client.get_submission(sub_id)
                except Exception as exc:  # fail-safe: unknown ⇒ still in-flight (never over-launch)
                    logger.debug(
                        "Poll error for submission %s (treating as in-flight): %s", sub_id, exc
                    )
                    continue
                if is_submission_terminal(submission):
                    outcome = classify(client, params.entity_type, sid, params.pipeline, submission)
                    result.outcomes[sid] = outcome
                    del in_flight[sub_id]
                    logger.info("Done %s (submission %s) → %s.", sid, sub_id, outcome.value)

    logger.info("Batch finished. %s", result.counts())
    return result
