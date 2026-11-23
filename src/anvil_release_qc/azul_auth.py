"""Azul access-token handling.

Azul does **not** accept a plain gcloud ADC token: its registered-user validation requires a Google
access token whose ``azp`` (authorized party) equals Azul's own OAuth client id (see
``DataBiosphere/azul`` ``src/azul/oauth2.py`` ``CredentialedClient.validate``). Azul's client is a
confidential web client whose secret lives only in Azul's backend, so we cannot mint such a token
ourselves — the token is obtained by an interactive browser login (the ``azul-login`` CLI helper) and
stored per operator in GCS at ``gs://<bucket>/azul_token/<username>.txt`` (``azul_token_uri``), where
every run -- local or in a Terra workflow -- reads it for the Sam-resolved user running it. It is a short-lived (~1 h), non-refreshable access token, used only in the (short)
Azul phase; the long Terra phase rides the auto-refreshing gcloud ADC token.

This module:
- locates, reads and writes each operator's token in GCS (``azul_token_uri`` / ``read_azul_token`` /
  ``write_azul_token``),
- validates a token via Google ``tokeninfo`` exactly as Azul does — ``azp``, account email, and
  expiry (``validate_azul_token``), so "passes our check" ⇒ "Azul will accept it",
- discovers the tier's expected ``azp`` from Azul's served Swagger initializer
  (``discover_azul_client_id``), so prod's client id need not be hardcoded,
- extracts a bearer token from a pasted Swagger ``curl`` command (``extract_bearer_token``).

See docs/export_import_flow.md → Authentication and risk R7.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from google.auth.credentials import Credentials

from anvil_release_qc.config import ResolvedTier
from anvil_release_qc.gcs import read_gcs_text_if_exists, write_gcs_text
from anvil_release_qc.logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

#: Default GCS bucket holding each operator's Azul token (a secure, auth-domain-protected Terra
#: workspace bucket). The WDL's ``azul_token_bucket`` input / ``--azul-token-bucket`` flag override it.
DEFAULT_AZUL_TOKEN_BUCKET = "fc-secure-886bea29-8c4e-4568-920a-a8cfee18e659"

#: Folder within the bucket that holds the per-user token files.
AZUL_TOKEN_PREFIX = "azul_token"

# Google's token introspection endpoint — the same one Azul's validate() uses.
TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"

# Required remaining lifetime (seconds) before a run. Azul tokens last ~1 h; for batch use this
# budgets ~30 min for the operator to set up/launch the batch and ~30 min for Azul's work -- large
# AnVIL studies and concurrent indexing load can make the Azul phase slow. Fail fast (re-run
# azul-login) rather than expire mid-run.
MIN_TOKEN_SECONDS_FOR_RUN = 1800  # 30 minutes

# Azul's health endpoint reporting the global indexing backlog. During a (re)index the manifest APIs
# still return 200 but with an EMPTY/incomplete manifest (Azul is unaware of the in-progress index), so
# QC would run against bad data; a non-zero backlog on either counter means indexing is in progress.
HEALTH_PROGRESS_PATH = "/health/progress"

# A Google OAuth client id, e.g. "5615...-3cv4....apps.googleusercontent.com".
_CLIENT_ID_RE = re.compile(r"([0-9]+-[A-Za-z0-9_]+\.apps\.googleusercontent\.com)")
# A bearer token in a pasted "Authorization: Bearer <token>" / curl line.
_BEARER_RE = re.compile(r"(?i)bearer\s+([A-Za-z0-9._~+/\-]+=*)")


class AzulTokenError(RuntimeError):
    """Raised when the Azul token is missing, malformed, or fails validation."""


class AzulReindexingError(RuntimeError):
    """Raised when Azul is (re)indexing (or unhealthy), so a manifest would be empty/incomplete.

    ade-qc must not run during an Azul (re)index: the manifest API returns 200 with an empty/incomplete
    manifest (it does not signal indexing state), which yields unreliable QC and a confusing downstream
    import "not found or empty" failure. This is retryable -- re-run after indexing completes.
    """


def tokeninfo(access_token: str) -> dict:
    """Return Google's ``tokeninfo`` for an access token (``azp``, ``aud``, ``email``, ``exp`` …)."""
    resp = requests.get(TOKENINFO_URL, params={"access_token": access_token}, timeout=30)
    if resp.status_code == 400:
        raise AzulTokenError(
            "Token is not valid (Google tokeninfo returned 400 — expired or malformed)."
        )
    resp.raise_for_status()
    return resp.json()


def discover_azul_client_id(azul_base_url: str) -> str:
    """Read the tier's Azul OAuth client id from Azul's served Swagger initializer.

    Azul injects ``const client_id = "<id>.apps.googleusercontent.com";`` into
    ``/swagger/swagger-initializer.js``; using it as the expected ``azp`` avoids hardcoding per-tier
    client ids (notably prod's).
    """
    url = f"{azul_base_url.rstrip('/')}/swagger/swagger-initializer.js"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    match = _CLIENT_ID_RE.search(resp.text)
    if not match:
        raise AzulTokenError(f"Could not find the Azul OAuth client id at {url}.")
    return match.group(1)


def expected_client_id(tier: ResolvedTier) -> str:
    """The ``azp`` an Azul token must carry: the tier's configured client id, else auto-discovered."""
    return tier.azul_oauth2_client_id or discover_azul_client_id(tier.url("azul"))


def azul_indexing_backlog(azul_base_url: str, *, timeout: int = 30) -> tuple[bool, int, int]:
    """Query Azul ``/health/progress``; return ``(up, unindexed_bundles, unindexed_documents)``.

    ``up`` is Azul's top-level health flag; the two counts are the outstanding indexing backlog (both 0
    when indexing is idle). Raises ``requests.RequestException`` for a network error / non-200 and
    ``ValueError`` for a malformed body (non-JSON, or a JSON shape/type we cannot parse). Callers that
    want to fail-open on an unreachable *or malformed* health endpoint catch both around this call -- a
    health-check hiccup must never turn into a false QC verdict.
    """
    url = f"{azul_base_url.rstrip('/')}{HEALTH_PROGRESS_PATH}"
    resp = requests.get(url, headers={"accept": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
        progress = data.get("progress") or {}
        up = bool(data.get("up", False))
        bundles = int(progress.get("unindexed_bundles", 0) or 0)
        documents = int(progress.get("unindexed_documents", 0) or 0)
    except (AttributeError, TypeError, ValueError) as exc:
        # A valid-JSON-but-unexpected shape (non-object body, non-dict "progress", non-integer counts)
        # would otherwise raise AttributeError/TypeError, which the callers' fail-open ``except`` does
        # NOT catch -- so a malformed health response would escape and become a false QC FAIL. Normalize
        # every parse failure to ValueError, which callers catch and fail-open on.
        raise ValueError(f"unexpected /health/progress response: {exc}") from exc
    return up, bundles, documents


def assert_azul_not_reindexing(azul_base_url: str) -> None:
    """Raise ``AzulReindexingError`` if Azul is (re)indexing (any non-zero backlog) or reports not up.

    Strict-zero by design: any outstanding backlog means a dataset may be empty/incomplete in Azul, so QC
    must not run (a false block is safely retryable; a false proceed produces bad QC). A network error on
    the health endpoint is *not* swallowed here -- callers decide whether to fail-open on an unreachable
    health check. See docs/qc_batch_runner.md ("Running under Terra load" / Azul reindexing).
    """
    url = f"{azul_base_url.rstrip('/')}{HEALTH_PROGRESS_PATH}"
    up, bundles, documents = azul_indexing_backlog(azul_base_url)
    if not up:
        raise AzulReindexingError(
            f"Azul reports not healthy (up=false) at {url}; not exporting. Re-run when Azul is up."
        )
    if bundles or documents:
        raise AzulReindexingError(
            f"Azul is (re)indexing (unindexed_bundles={bundles}, unindexed_documents={documents} at "
            f"{url}): the exported manifest would be empty or incomplete and QC results unreliable. "
            "Aborting before export; re-run after reindexing completes."
        )
    logger.info(
        "Azul indexing idle (unindexed_bundles=0, unindexed_documents=0 at %s); proceeding.", url
    )


def validate_azul_token(
    token: str,
    *,
    expected_azp: str,
    min_remaining_seconds: int = 0,
) -> dict:
    """Validate an Azul access token the way Azul does; raise ``AzulTokenError`` if it won't work.

    Checks (via ``tokeninfo``): ``azp`` == the Azul OAuth client, and at least ``min_remaining_seconds``
    of life left. Returns the tokeninfo dict on success; callers log its ``email`` (the token's owner)
    for the audit trail. The account itself is not restricted -- Azul grants or denies data access.
    """
    info = tokeninfo(token)
    azp = info.get("azp")
    if azp != expected_azp:
        raise AzulTokenError(
            f"Token's azp '{azp}' does not match Azul's OAuth client '{expected_azp}'. "
            "Azul will reject it — copy the token from the Azul Swagger UI (it must be issued to "
            "Azul's own client), e.g. via `anvil-release-qc azul-login`."
        )
    remaining = int(info.get("expires_in", 0))
    if remaining <= min_remaining_seconds:
        raise AzulTokenError(
            f"Azul token expires in {remaining}s (need > {min_remaining_seconds}s). "
            "Refresh it with `anvil-release-qc azul-login`."
        )
    return info


def extract_bearer_token(text: str) -> str:
    """Pull the token out of a pasted ``Authorization: Bearer <token>`` / Swagger ``curl`` line.

    If no ``Bearer`` is present, accept the input as the raw token when it is a single token-like
    string (the common "just pasted the token" case).
    """
    match = _BEARER_RE.search(text)
    if match:
        return match.group(1)
    stripped = text.strip()
    if stripped and not re.search(r"\s", stripped):
        return stripped
    raise AzulTokenError(
        "Could not find a token. Paste the access token, or the Swagger 'curl' line containing "
        "'Authorization: Bearer …'."
    )


def token_from_file_text(text: str) -> Optional[str]:
    """Extract the token from Azul-token-file contents: the first non-comment, non-blank line.

    The token files ``azul-login`` writes carry a ``#``-comment header (account/azp/expiry) above the
    token line, so the whole file text is NOT the token. Returns ``None`` when the text is empty or only
    comments. Used by both the local file reader and the batch runner's GCS reader, so the two parse
    token files identically. The token is opaque and not shape-checked here; ``validate_azul_token`` does.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def azul_token_uri(user_email: str, bucket: Optional[str] = None) -> str:
    """Where ``user_email``'s Azul token lives: ``gs://<bucket>/azul_token/<username>.txt``.

    ``username`` is the part of the (Sam-resolved, human) Terra user email before the ``@`` -- so a
    workflow running as a pet service account finds its owner's token.
    """
    username = user_email.split("@", 1)[0].strip()
    if not username:
        raise AzulTokenError(f"Cannot derive a username from {user_email!r}.")
    return f"gs://{bucket or DEFAULT_AZUL_TOKEN_BUCKET}/{AZUL_TOKEN_PREFIX}/{username}.txt"


def read_azul_token(creds: Credentials, token_uri: str) -> Optional[str]:
    """Read the Azul access token at ``token_uri``, or ``None`` if there is none.

    Returns ``None`` when the object does not exist or holds no token line (the caller then calls Azul
    anonymously -- open-access only). Raises ``AzulTokenError`` when the object can't be read (e.g. no
    read access to the bucket). The token is not shape-checked here; ``validate_azul_token`` does that.
    """
    try:
        text = read_gcs_text_if_exists(creds, token_uri)  # SECRET -- never log
    except requests.RequestException as exc:
        raise AzulTokenError(
            f"Could not read the Azul token at {token_uri}: {exc}. Does the running account have read "
            "access to that bucket?"
        ) from exc
    return token_from_file_text(text) if text is not None else None


def write_azul_token(
    creds: Credentials, token_uri: str, tier: ResolvedTier, token: str, info: dict
) -> str:
    """Upload the validated token to ``token_uri`` with a human-readable header; return the URI."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=int(info.get("expires_in", 0)))
    header = (
        f"# Azul access token for tier '{tier.name}' — SECRET, DO NOT COMMIT OR SHARE.\n"
        f"# account: {info.get('email')}   azp: {info.get('azp')}\n"
        f"# acquired: {now.isoformat(timespec='seconds')}   "
        f"expires: {expires.isoformat(timespec='seconds')}\n"
        f"# refresh with:  anvil-release-qc azul-login --tier {tier.name}\n"
    )
    try:
        write_gcs_text(creds, token_uri, header + token + "\n")
    except requests.RequestException as exc:
        raise AzulTokenError(
            f"Could not upload the Azul token to {token_uri}: {exc}. Does your account have write "
            "access to that bucket?"
        ) from exc
    return token_uri
