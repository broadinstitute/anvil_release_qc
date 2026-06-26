"""Azul access-token handling.

Azul does **not** accept a plain gcloud ADC token: its registered-user validation requires a Google
access token whose ``azp`` (authorized party) equals Azul's own OAuth client id (see
``DataBiosphere/azul`` ``src/azul/oauth2.py`` ``CredentialedClient.validate``). Azul's client is a
confidential web client whose secret lives only in Azul's backend, so we cannot mint such a token
ourselves — the token is obtained by an interactive browser login (the ``azul-login`` CLI helper) and
stored per tier. It is a short-lived (~1 h), non-refreshable access token, used only in the (short)
Azul phase; the long Terra phase rides the auto-refreshing gcloud ADC token.

This module:
- reads/writes the per-tier token file (``read_azul_token`` / ``write_azul_token``),
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
from pathlib import Path
from typing import Collection, Optional

import requests

from .config import ResolvedTier
from .logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

# Google's token introspection endpoint — the same one Azul's validate() uses.
TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"

# Required remaining lifetime (seconds) before a run. Azul tokens last ~1 h; for batch use this
# budgets ~30 min for the operator to set up/launch the batch and ~30 min for Azul's work -- large
# AnVIL studies and concurrent indexing load can make the Azul phase slow. Fail fast (re-run
# azul-login) rather than expire mid-run.
MIN_TOKEN_SECONDS_FOR_RUN = 1800  # 30 minutes

# A Google OAuth client id, e.g. "5615...-3cv4....apps.googleusercontent.com".
_CLIENT_ID_RE = re.compile(r"([0-9]+-[A-Za-z0-9_]+\.apps\.googleusercontent\.com)")
# A bearer token in a pasted "Authorization: Bearer <token>" / curl line.
_BEARER_RE = re.compile(r"(?i)bearer\s+([A-Za-z0-9._~+/\-]+=*)")


class AzulTokenError(RuntimeError):
    """Raised when the Azul token is missing, malformed, or fails validation."""


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


def validate_azul_token(
    token: str,
    *,
    expected_azp: str,
    allowed_emails: Optional[Collection[str]] = None,
    min_remaining_seconds: int = 0,
) -> dict:
    """Validate an Azul access token the way Azul does; raise ``AzulTokenError`` if it won't work.

    Checks (via ``tokeninfo``): ``azp`` == the Azul OAuth client, that the token's account is one of
    ``allowed_emails`` (when given -- supporting multiple authorized operators), and at least
    ``min_remaining_seconds`` of life left. Returns the tokeninfo dict on success.
    """
    info = tokeninfo(token)
    azp = info.get("azp")
    if azp != expected_azp:
        raise AzulTokenError(
            f"Token's azp '{azp}' does not match Azul's OAuth client '{expected_azp}'. "
            "Azul will reject it — copy the token from the Azul Swagger UI (it must be issued to "
            "Azul's own client), e.g. via `anvil-data-qc azul-login`."
        )
    if allowed_emails:
        token_email = (info.get("email") or "").lower()
        allowed = {e.lower() for e in allowed_emails}
        if token_email not in allowed:
            raise AzulTokenError(
                f"Token belongs to '{info.get('email')}', which is not an authorized account for this "
                f"tier ({sorted(allowed)}). Log in to Azul as an authorized account."
            )
    remaining = int(info.get("expires_in", 0))
    if remaining <= min_remaining_seconds:
        raise AzulTokenError(
            f"Azul token expires in {remaining}s (need > {min_remaining_seconds}s). "
            "Refresh it with `anvil-data-qc azul-login`."
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


def read_azul_token(tier: ResolvedTier, *, override_path: Optional[str] = None) -> Optional[str]:
    """Read the Azul access token, or ``None`` if none is configured.

    ``override_path`` (e.g. a WDL-localized token file passed via ``--azul-token-file``) takes
    precedence over the tier's configured ``azul_token_file``. Returns ``None`` when neither is set, or
    the file is empty / only comments (the caller may then call Azul anonymously — open-access only).
    Otherwise returns the first non-comment, non-blank line (the token). The token is an opaque Google
    access token, so it is not shape-checked here; ``validate_azul_token`` does the real validation.
    """
    token_file = override_path or tier.azul_token_file
    if not token_file:
        return None
    path = Path(token_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Azul token file not found: {path}. Run `anvil-data-qc azul-login`."
        )
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None  # present but empty / only comments -> anonymous


def write_azul_token(tier: ResolvedTier, token: str, info: dict) -> Path:
    """Write the validated token to the tier's token file (0600) with a human-readable header."""
    if not tier.azul_token_file:
        raise AzulTokenError(
            f"Tier '{tier.name}' has no 'azul_token_file' configured; set it before azul-login."
        )
    path = Path(tier.azul_token_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=int(info.get("expires_in", 0)))
    header = (
        f"# Azul access token for tier '{tier.name}' — SECRET, DO NOT COMMIT.\n"
        f"# account: {info.get('email')}   azp: {info.get('azp')}\n"
        f"# acquired: {now.isoformat(timespec='seconds')}   "
        f"expires: {expires.isoformat(timespec='seconds')}\n"
        f"# refresh with:  anvil-data-qc azul-login --tier {tier.name}\n"
    )
    path.write_text(header + token + "\n")
    path.chmod(0o600)
    return path
