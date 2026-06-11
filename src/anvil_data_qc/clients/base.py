"""Base HTTP client: shared auth + centralized, redacted request/response logging.

Every service client (Azul, TDR, Rawls, Firecloud) builds on this so auth and logging are uniform.
The per-service clients are added during the spike as the flow steps are implemented.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests
from google.auth.credentials import Credentials

from ..auth import bearer_token
from ..logging_setup import LOGGER_NAME, correlation_fields, redact_headers

logger = logging.getLogger(LOGGER_NAME)


class BaseClient:
    """A thin wrapper over ``requests`` that injects the bearer token and logs every call."""

    def __init__(self, base_url: str, credentials: Credentials, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._timeout = timeout
        self._session = requests.Session()

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {bearer_token(self._credentials)}"}

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

        # Log the request with the bearer token redacted.
        logger.debug("%s %s", method, url)
        if params:
            logger.debug("  params: %s", params)
        logger.debug("  headers: %s", redact_headers(all_headers))
        if json_body is not None:
            logger.debug("  body: %s", json.dumps(json_body))

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
