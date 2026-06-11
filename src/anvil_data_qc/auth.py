"""Authentication via Google Application Default Credentials (ADC), with a tier identity guard.

One standard Google user account per tier. Locally the credentials come from a per-tier saved ADC
file; in deployment, from the ambient runtime identity. The same credentials yield both REST bearer
tokens (Azul, Terra) and the BigQuery client. Before any tier work, the identity guard asserts the
active credential's email matches the configured email for the selected tier.
"""

from __future__ import annotations

import logging
from pathlib import Path

import google.auth
import requests
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from .config import ResolvedTier

logger = logging.getLogger("anvil_data_qc")

# Scopes granted by a plain `gcloud auth application-default login` (openid, email, cloud-platform).
SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
)

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class IdentityMismatchError(RuntimeError):
    """Raised when the active credential's identity does not match the configured tier email."""


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


def assert_tier_identity(creds: Credentials, tier: ResolvedTier) -> str:
    """Identity guard: abort unless the active credential's email matches the tier's configured email."""
    actual = active_identity_email(creds)
    if actual.lower() != tier.email.lower():
        raise IdentityMismatchError(
            f"Active identity '{actual}' does not match configured '{tier.email}' for tier "
            f"'{tier.name}'. Refusing to proceed (wrong-tier guard)."
        )
    logger.info("Identity guard OK: %s (tier '%s').", actual, tier.name)
    return actual
