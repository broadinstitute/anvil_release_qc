"""Minimal authorized read/write of a small GCS text object (the Azul token file).

Each operator's Azul token lives at ``gs://<bucket>/azul_token/<username>.txt`` (see
``azul_auth.azul_token_uri``): ``azul-login`` uploads it, ``ade-qc`` reads it at the start of a run, and
the batch runner reads it to check its remaining TTL before launching. We use an ``AuthorizedSession``
over the ADC credentials (the same creds used for TDR/BigQuery/Terra; in a Terra workflow, the pet
service account) via the GCS JSON API -- no extra dependency (``google-auth`` already provides this),
and no need for ``google-cloud-storage``.

SECURITY: the content is the Azul access token -- a secret. Never log it; callers validate it
(``azul_auth.validate_azul_token``) and log only the resulting metadata (account/azp/expiry).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

_API = "https://storage.googleapis.com"


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
    token file); do not use for large blobs. The content may be sensitive -- do not log it. Raises
    ``requests.HTTPError`` on any HTTP failure (404 = no such object, 403 = no read access).
    """
    bucket, obj = parse_gs_uri(gs_uri)
    url = f"{_API}/storage/v1/b/{bucket}/o/{quote(obj, safe='')}?alt=media"
    resp = AuthorizedSession(credentials).get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def read_gcs_text_if_exists(
    credentials: Credentials, gs_uri: str, *, timeout: int = 30
) -> Optional[str]:
    """Like ``read_gcs_text``, but return ``None`` when the object does not exist (HTTP 404)."""
    bucket, obj = parse_gs_uri(gs_uri)
    url = f"{_API}/storage/v1/b/{bucket}/o/{quote(obj, safe='')}?alt=media"
    resp = AuthorizedSession(credentials).get(url, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def write_gcs_text(credentials: Credentials, gs_uri: str, text: str, *, timeout: int = 30) -> None:
    """Create or overwrite a small GCS text object with ``text`` (simple media upload).

    The content may be sensitive -- do not log it. Raises ``requests.HTTPError`` on failure (403 = no
    write access to the bucket).
    """
    bucket, obj = parse_gs_uri(gs_uri)
    url = f"{_API}/upload/storage/v1/b/{bucket}/o?uploadType=media&name={quote(obj, safe='')}"
    resp = AuthorizedSession(credentials).post(
        url,
        data=text.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=timeout,
    )
    resp.raise_for_status()
