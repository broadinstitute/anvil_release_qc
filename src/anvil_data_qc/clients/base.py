"""Base HTTP client: shared auth + centralized, redacted request/response logging.

Every service client (Azul, TDR, Rawls, Firecloud) builds on this so auth and logging are uniform.
The per-service clients are added during the spike as the flow steps are implemented.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Optional

import requests

from ..logging_setup import LOGGER_NAME, correlation_fields, redact_headers
from ..redaction import redact_text

logger = logging.getLogger(LOGGER_NAME)


class BaseClient:
    """A thin wrapper over ``requests`` that injects a bearer token and logs every call.

    ``token_provider`` is a callable returning the bearer token string -- a Google access token for
    most services, or the Azul personal access token (APAT) for Azul. The client is agnostic to where
    the token comes from.
    """

    def __init__(self, base_url: str, token_provider: Callable[[], str], timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._timeout = timeout
        self._session = requests.Session()

    def _auth_header(self) -> dict[str, str]:
        token = self._token_provider()
        # A falsy token means "send no auth" (e.g. anonymous Azul access to open-access data).
        return {"Authorization": f"Bearer {token}"} if token else {}

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict] = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        all_headers = {"Accept": "application/json", **(headers or {}), **self._auth_header()}

        # Log the request with secrets redacted: bearer token in headers, plus signed-URL
        # signatures / AWS creds / tokens that can appear in the URL, params, or body (e.g. the
        # importJob body carries a signed S3 PFB URL).
        logger.debug("%s %s", method, redact_text(url))
        if params:
            logger.debug("  params: %s", redact_text(str(params)))
        logger.debug("  headers: %s", redact_headers(all_headers))
        if json_body is not None:
            logger.debug("  body: %s", redact_text(json.dumps(json_body)))

        resp = self._session.request(
            method, url, params=params, json=json_body, headers=all_headers, timeout=self._timeout
        )

        # Log the response status and any correlation/trace ids.
        corr = correlation_fields(resp.headers)
        logger.debug(
            "  -> %s %s%s", resp.status_code, resp.reason or "", f"  {corr}" if corr else ""
        )
        resp.raise_for_status()
        return resp
