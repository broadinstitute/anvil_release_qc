"""Minimal authorized read of a small GCS text object (the Azul token file).

The batch runner needs to read the Azul token whose ``gs://`` path is stored in the workspace's Terra
Workspace Data (``anvil_data_qc_azul_token``), to check its remaining TTL before launching ade-qc. We
read it with an ``AuthorizedSession`` over the ADC credentials (the same creds used for TDR/BigQuery/
Terra) via the GCS JSON API -- no extra dependency (``google-auth`` already provides this), and no need
for ``google-cloud-storage``.

SECURITY: the returned content is the Azul access token -- a secret. Never log it; the caller validates
it (``azul_auth.validate_azul_token``) and logs only the resulting metadata (account/azp/expiry).
"""

from __future__ import annotations

from urllib.parse import quote

from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession


def parse_gs_uri(gs_uri: str) -> tuple[str, str]:
    """Split a ``gs://bucket/object/path`` into ``(bucket, object)``; raise on a malformed URI."""
    if not isinstance(gs_uri, str) or not gs_uri.startswith("gs://"):
        raise ValueError(f"Not a gs:// URI: {gs_uri!r}")
    bucket, _, obj = gs_uri[len("gs://") :].partition("/")
    if not bucket or not obj:
        raise ValueError(f"gs:// URI must include a bucket and object: {gs_uri!r}")
    return bucket, obj


def read_gcs_text(credentials: Credentials, gs_uri: str, *, timeout: int = 30) -> str:
    """Return the text content of a small GCS object, read with ``credentials`` (media download).

    Uses the GCS JSON API ``objects.get`` with ``alt=media``. Intended for tiny text objects (the Azul
    token file); do not use for large blobs. The content may be sensitive -- do not log it.
    """
    bucket, obj = parse_gs_uri(gs_uri)
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(obj, safe='')}?alt=media"
    resp = AuthorizedSession(credentials).get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text
