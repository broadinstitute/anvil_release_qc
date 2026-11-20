"""Secret redaction for logs.

The HTTP layer logs every request URL, params and body, so those strings must never carry secrets. The
only secrets that can appear are bearer tokens and the signing parameters of signed URLs (the PFB / TDR
manifest URLs grant direct access to controlled data). Both are masked here; host, path and every other
parameter are preserved.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"

# Query / form / JSON parameter names whose value is a secret (value redacted, name kept).
# Query-parameter names whose value is a secret (value redacted, name kept): signed-URL signatures and
# credentials (GCS + AWS) and OAuth tokens.
SENSITIVE_PARAM_NAMES = frozenset(
    {
        "signature",
        "x-goog-signature",
        "x-goog-credential",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "awsaccesskeyid",
        "sig",
        "access_token",
        "id_token",
        "refresh_token",
        "token",
    }
)

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*")
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def redact_url(url: str) -> str:
    """Mask the values of sensitive query parameters in a URL."""
    if "?" not in url:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.strip().lower() in SENSITIVE_PARAM_NAMES for k, _ in pairs):
        return url
    query = urlencode(
        [(k, REDACTED if k.strip().lower() in SENSITIVE_PARAM_NAMES else v) for k, v in pairs]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def redact_text(text: str) -> str:
    """Redact bearer tokens and signed-URL secrets in any free text (including JSON bodies)."""
    if not text:
        return text
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), text)
