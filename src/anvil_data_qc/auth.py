"""Authentication via Google Application Default Credentials (ADC), with a tier identity guard.

One standard Google user account per tier. Locally the credentials come from a per-tier saved ADC
file; in deployment, from the ambient runtime identity. The same credentials yield both REST bearer
tokens (Azul, Terra) and the BigQuery client. Before any tier work, the identity guard asserts the
active credential's email matches the configured email for the selected tier.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

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

# An APAT is a JWT: three base64url segments, the header beginning with "eyJ" (base64url of '{"').
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+$")


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


def read_azul_apat(tier: ResolvedTier) -> Optional[str]:
    """Read the Azul personal access token (APAT) for the tier, or None if none is configured.

    Returns None when ``azul_apat_file`` is unset, or the file is empty / only comments; the caller
    may then call Azul without a token (works for open-access datasets). Otherwise '#' comment lines
    are ignored and the remaining content may be either the raw token or the JSON response from
    Azul's ``GET /user/token`` (``{"token": "..."}``). Azul accepts the APAT where it rejects gcloud.
    """
    if not tier.azul_apat_file:
        return None
    path = Path(tier.azul_apat_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Azul APAT file not found: {path}")
    content = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    ).strip()
    if not content:
        # Present but empty / only comments -> treat as "no APAT" (anonymous), same as unset.
        return None
    if content.startswith("{"):  # the full /user/token JSON response
        try:
            content = json.loads(content)["token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Could not extract 'token' from APAT JSON in {path}: {exc}") from exc
    token = content.strip()
    if not _JWT_RE.match(token):
        raise ValueError(
            f"The Azul APAT in {path} does not look like a JWT (expected three base64url segments, "
            "'eyJ….….…'). Re-paste the token from Azul's GET /user/token."
        )
    return token
