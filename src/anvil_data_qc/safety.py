"""Safety controls for handling controlled-access data.

The Azul PFB signed URL grants direct access to **all of an NIH study's controlled-access tabular
data**. Mishandling it -- e.g. importing a production signed URL into the insecure dev tier, or any
unintended destination -- would be a Federal Data Management Incident (DMI). Controlled-access use was
**reviewed and approved by the Broad security team** (no restrictions -- approved for all AnVIL data);
it is gated behind an explicit, default-off opt-in (``allow_controlled_access``), so the default stays
fail-closed and every controlled-access run is a deliberate, audit-logged choice.

This module provides four layers of defense-in-depth (do not weaken any without security review):

1. **Consent gate** -- by default the pipeline runs only on **explicitly open-access** snapshots: the
   TDR snapshot's ``consentCode`` must be exactly ``"NRES"`` (No Restrictions). Fails closed: empty,
   missing, differently-cased, or any other value is treated as controlled/unknown and blocked, in
   every tier, **before any PFB signed URL is generated**. A controlled-access run must explicitly set
   ``allow_controlled_access`` (see ``assert_consent_permitted``), which permits any consentCode and
   emits a controlled-access audit log; layers 2-4 still apply.
2. **Secret containment** -- the signed URL is wrapped in ``SignedUrl``, which redacts on
   repr/str/format and is not JSON-serializable. The raw value is reachable only via ``.reveal()``,
   called at exactly one site (the importJob POST body), so it cannot leak into logs/exceptions/
   returns/new code by accident.
3. **Hand-off verification** -- ``verify_pfb_handoff`` refuses unless the import destination is the
   run tier's canonical Firecloud host, and (when configured) the signed URL originates from the run
   tier's allow-listed bucket -- so a URL from one tier can never be delivered to another.
4. **Bearer-token destination pinning** -- the HTTP base client (``clients/base.py``) raises
   ``BearerDestinationError`` if a bearer token would be sent to any host but the client's own HTTPS
   host, so a token (reusable, broad access) can only ever reach its intended service.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlsplit

from .logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


class SafetyError(RuntimeError):
    """Base for safety-gate aborts (the CLI presents these cleanly and exits non-zero)."""


# --- Layer 1: consent gate (open-access only) ---

#: The TDR ``consentCode`` meaning "No Restrictions" -- the only value treated as open-access.
OPEN_ACCESS_CONSENT_CODE = "NRES"


class ControlledAccessNotApprovedError(SafetyError):
    """Raised to block the pipeline on any snapshot that is not explicitly open-access."""


class TdrControlledAccessNotYetEnabledError(SafetyError):
    """Raised to block controlled-access ``tdr-qc`` until its permission/auth-domain behavior is confirmed."""


def is_open_access(consent_code: Optional[str]) -> bool:
    """True only when ``consent_code`` is exactly ``"NRES"``.

    Fails closed: empty string, ``None``, a different case, or any other value returns ``False``
    (controlled-access or unknown). Never infer open-access from a missing/empty consent code.
    """
    return consent_code == OPEN_ACCESS_CONSENT_CODE


def assert_open_access(snapshot_id: str, consent_code: Optional[str]) -> None:
    """Raise ``ControlledAccessNotApprovedError`` unless the snapshot is explicitly open-access."""
    if not is_open_access(consent_code):
        raise ControlledAccessNotApprovedError(
            f"Snapshot {snapshot_id} has consentCode={consent_code!r}, not "
            f"'{OPEN_ACCESS_CONSENT_CODE}' (open-access), and controlled-access was not enabled for "
            "this run. To run on controlled-access data, set allow_controlled_access (the "
            "--allow-controlled-access CLI flag / WDL input). Aborting before any signed URL is generated."
        )


def assert_consent_permitted(
    snapshot_id: str, consent_code: Optional[str], *, allow_controlled_access: bool
) -> None:
    """Consent gate: permit the run, or raise ``ControlledAccessNotApprovedError``.

    - ``allow_controlled_access=False`` (default): only explicitly open-access snapshots pass --
      raises unless ``consent_code == "NRES"``.
    - ``allow_controlled_access=True``: controlled-access was explicitly authorized for this run (the
      Broad security team approved all AnVIL data), so **any** consentCode is permitted.

    Open-access-vs-controlled labeling, and the token requirement for controlled data, are handled by
    the caller keyed off the actual ``consent_code`` -- so an open-access snapshot is never mislabeled
    "controlled" merely because the opt-in was set. Layers 2-4 apply regardless.
    """
    if allow_controlled_access:
        return
    assert_open_access(snapshot_id, consent_code)


def assert_tdr_qc_permitted(
    snapshot_id: str, consent_code: Optional[str], *, allow_controlled_access: bool
) -> None:
    """Consent gate for ``tdr-qc``: open-access only, for now.

    Applies the same default-off ``allow_controlled_access`` gate as ade-qc, but **additionally blocks
    controlled-access tdr-qc entirely -- even when ``allow_controlled_access`` is set** -- until the
    correct Terra importJob ``tdrSyncPermissions`` value for controlled data (and whether ``tdrexport``
    applies the snapshot's auth domain) is confirmed. The open-access capture used
    ``tdrSyncPermissions: false``; mirroring that onto controlled data could leave the workspace
    **under-protected (a Federal DMI)**. See docs/tdr_export_flow.md and risk R8.

    Raises ``ControlledAccessNotApprovedError`` (opt-in off) or ``TdrControlledAccessNotYetEnabledError``
    (opt-in on but controlled) for any non-open-access snapshot.
    """
    assert_consent_permitted(
        snapshot_id, consent_code, allow_controlled_access=allow_controlled_access
    )
    if not is_open_access(consent_code):
        raise TdrControlledAccessNotYetEnabledError(
            f"Snapshot {snapshot_id} is controlled-access (consentCode={consent_code!r}). tdr-qc does "
            "not yet support controlled-access data: the correct importJob 'tdrSyncPermissions' value "
            "for controlled data (and whether tdrexport applies the auth domain) must be confirmed "
            "first -- see docs/tdr_export_flow.md. Only open-access snapshots are supported by tdr-qc "
            "currently."
        )


# --- Layer 2: secret containment ---


class SignedUrl:
    """Opaque holder for the sensitive Azul PFB signed URL.

    The raw value grants direct read of controlled-access data, so it must not leak. This wrapper
    **redacts on repr/str/format** and is **not JSON-serializable**; the raw value is reachable only
    via :meth:`reveal`, which must be called at exactly one place -- the importJob POST body.
    :attr:`location` (scheme+host+path, no query) and :attr:`host` are safe to log and to use for the
    destination/provenance checks.
    """

    __slots__ = ("_url",)

    def __init__(self, url: str) -> None:
        self._url = url

    def reveal(self) -> str:
        """Return the raw signed URL. Call ONLY at the single import hand-off site (firecloud.py)."""
        return self._url

    @property
    def host(self) -> str:
        return urlsplit(self._url).netloc

    @property
    def location(self) -> str:
        """``scheme://host/path`` -- the URL with the secret query string removed (safe to log)."""
        parts = urlsplit(self._url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def __repr__(self) -> str:
        return f"<SignedUrl {self.location} ?…redacted>"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return self.__repr__()


# --- Layer 3: hand-off verification (right destination, right provenance) ---

#: Per-tier allow-list of signed-URL location prefixes (the AnVIL storage bucket the PFB comes from).
#: Enforced when set for a tier; warned (not blocked) when empty. Destination check + consent gate
#: still apply regardless.
SIGNED_URL_ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    # Verified against real signed URLs (2026-06-14): dev from the 1000G run, prod from an interactive
    # open-access flow. Same host (path-style s3.amazonaws.com), parallel dev/prod buckets. Each prefix
    # carries the FULL bucket name and a trailing "/", so the startswith check is anchored at the
    # bucket boundary -- a sibling bucket (e.g. "...-anvildev-attacker") cannot satisfy it. Update these
    # if the AnVIL storage bucket name or its region ever changes.
    "dev": ("https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-dev-storage-anvildev.us-east-1/",),
    "prod": (
        "https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-prod-storage-anvilprod.us-east-1/",
    ),
}


def tdr_export_allowed_prefixes(bq_project: str) -> tuple[str, ...]:
    """Allowed signed-URL provenance for a TDR snapshot export (layer 3, used by tdr-qc).

    TDR writes a snapshot export to ``gs://<bq_project>-snapshot-export-bucket/<jobId>/...`` and returns
    signed https URLs on ``storage.googleapis.com``. The bucket is derived from the snapshot's own
    BigQuery project (verified against a real export), so provenance is pinned **per-snapshot** -- tighter
    than a fixed per-tier list. The trailing ``/`` anchors the prefix at the bucket boundary. Pass the
    result to ``verify_pfb_handoff(allowed_prefixes=...)``.
    """
    return (f"https://storage.googleapis.com/{bq_project}-snapshot-export-bucket/",)


class PfbHandoffError(SafetyError):
    """Raised when the signed URL would be delivered to an unverified destination or source."""


class SignedUrlDestinationError(PfbHandoffError):
    """The import destination host is not the run tier's canonical Firecloud host."""


class SignedUrlProvenanceError(PfbHandoffError):
    """The signed URL did not originate from the run tier's allow-listed bucket."""


def verify_pfb_handoff(
    signed_url: SignedUrl,
    *,
    tier_name: str,
    expected_firecloud_host: str,
    destination_host: str,
    allowed_prefixes: tuple[str, ...],
) -> None:
    """Verify the signed URL is delivered only to the intended, same-tier import destination.

    - **Destination integrity:** refuse unless ``destination_host`` equals the tier's canonical
      Firecloud host (so the URL goes only to this tier's import service).
    - **Provenance:** when ``allowed_prefixes`` is set, the signed URL's location must start with one
      of them (so a URL from another tier/source is rejected); when empty, warn but do not block.
    - **Audit:** log the destination host (no secret) on success.

    Raises ``SignedUrlDestinationError`` / ``SignedUrlProvenanceError`` (both ``PfbHandoffError``).
    """
    if destination_host != expected_firecloud_host:
        raise SignedUrlDestinationError(
            f"Import destination host {destination_host!r} does not match the expected Firecloud "
            f"host {expected_firecloud_host!r} for tier {tier_name!r}. Refusing to deliver the PFB "
            "signed URL."
        )
    if allowed_prefixes:
        if not any(signed_url.location.startswith(prefix) for prefix in allowed_prefixes):
            raise SignedUrlProvenanceError(
                f"Signed URL {signed_url.location!r} is not from an allow-listed source for tier "
                f"{tier_name!r}. Refusing to deliver it."
            )
    else:
        logger.warning(
            "No signed-URL provenance allow-list configured for tier %r; source not verified "
            "(destination check and consent gate still apply). Populate "
            "SIGNED_URL_ALLOWED_PREFIXES[%r].",
            tier_name,
            tier_name,
        )
    logger.info(
        "PFB hand-off verified: delivering to Firecloud host %s (tier %s).",
        destination_host,
        tier_name,
    )


# --- Bearer-token destination pinning (enforced in clients/base.py) ---


class BearerDestinationError(SafetyError):
    """Raised when a bearer token would be sent to a host other than the client's own HTTPS host."""
