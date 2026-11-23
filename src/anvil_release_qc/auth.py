"""Authentication via Google Application Default Credentials (ADC), plus identity resolution for the log.

Locally the credentials come from a per-tier saved ADC file (optional local config) or the ambient ADC;
in deployment, from the ambient runtime identity (a Terra **pet service account**). The same credentials
yield both REST bearer tokens (Azul, Terra) and the BigQuery client. Before any tier work,
``resolve_tier_identity`` resolves the **canonical Terra user** behind the active credentials via Sam
(``/api/users/v2/self``, which maps a pet service account to its owning human user) and logs it for the
audit trail. There is no allowlist: who may act on what is enforced by Terra/TDR/Azul permissions.
Resolving via Sam still fails fast when the credentials are not a registered Terra user of the tier
(e.g. dev credentials pointed at prod).
"""

from __future__ import annotations

import logging
from pathlib import Path

import google.auth
import requests
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from anvil_release_qc.config import ResolvedTier

logger = logging.getLogger("anvil_release_qc")

# Scopes granted by a plain `gcloud auth application-default login` (openid, email, cloud-platform).
SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
)

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

#: Sam endpoint that maps an access token (a human login OR a Terra pet service account) to the
#: registered Terra user, returning their human email -- so a pet SA is logged as its owning user
#: (Sam Swagger: GET /api/users/v2/self).
SAM_SELF_PATH = "/api/users/v2/self"


def credentials_for_tier(tier: ResolvedTier) -> Credentials:
    """Load ADC credentials for the tier: a per-tier file if configured, else the ambient ADC."""
    if tier.adc_credentials_file:
        path = str(Path(tier.adc_credentials_file).expanduser())
        creds, _ = google.auth.load_credentials_from_file(path, scopes=SCOPES)
    else:
        creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def bearer_token(creds: Credentials) -> str:
    """Refresh (if needed) and return the OAuth2 access token for `Authorization: Bearer` headers."""
    if not creds.valid:
        creds.refresh(Request())
    return creds.token


def active_identity_email(creds: Credentials) -> str:
    """Resolve the Google account email behind the active credentials (OIDC userinfo)."""
    token = bearer_token(creds)
    resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    email = resp.json().get("email")
    if not email:
        raise RuntimeError("Could not resolve the active account email from the userinfo endpoint.")
    return email


def terra_user_email(creds: Credentials, sam_url: str) -> str:
    """Resolve the canonical Terra user email behind the active credentials, via Sam.

    Works for both a human ADC login and a Terra pet service account: Sam's ``/api/users/v2/self``
    maps the access token to the registered Terra user and returns their (human) email. This lets the
    audit log record a pet service account as its owning human user.
    """
    token = bearer_token(creds)
    url = f"{sam_url.rstrip('/')}{SAM_SELF_PATH}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not resolve the Terra user from Sam ({url}): {exc}. Is the active account a "
            "registered Terra user, and is Sam reachable?"
        ) from exc
    email = resp.json().get("email")
    if not email:
        raise RuntimeError("Sam /api/users/v2/self returned no email for the active identity.")
    return email


def resolve_tier_identity(creds: Credentials, tier: ResolvedTier) -> str:
    """Resolve and log the Terra user the active credentials operate as; return its email.

    Resolves the canonical Terra user via Sam (mapping a pet service account to its owning human user);
    the raw ADC identity (e.g. the pet SA in a Terra workflow) is logged alongside for the audit trail.
    Raises ``RuntimeError`` if Sam cannot resolve the credentials to a Terra user on this tier.
    """
    # Raw ADC account (e.g. the pet SA) -- audit-only, so best-effort: never let this lookup block a run.
    try:
        adc_identity = active_identity_email(creds)
    except (requests.RequestException, RuntimeError) as exc:
        logger.debug("Could not resolve the raw ADC identity for the audit log: %s", exc)
        adc_identity = "unknown"
    terra_user = terra_user_email(creds, tier.url("sam"))
    logger.info(
        "Running as Terra user %s (ADC identity %s; tier '%s').",
        terra_user,
        adc_identity,
        tier.name,
    )
    return terra_user
