"""Base HTTP client: shared auth + centralized, redacted request/response logging.

Every service client (Azul, TDR, Rawls, Firecloud) builds on this so auth and logging are uniform.
The per-service clients are added during the spike as the flow steps are implemented.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from anvil_release_qc.logging_setup import LOGGER_NAME, log_request, log_response
from anvil_release_qc.safety import BearerDestinationError

logger = logging.getLogger(LOGGER_NAME)


class BaseClient:
    """A thin wrapper over ``requests`` that injects a bearer token and logs every call.

    ``token_provider`` is a callable returning the bearer token string -- the gcloud ADC token for
    most services, or the Azul access token for Azul (or ``None`` for anonymous/open-access Azul).
    The client is agnostic to where the token comes from.
    """

    def __init__(self, base_url: str, token_provider: Callable[[], str], timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self._host = urlsplit(self.base_url).netloc
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
        timeout: Optional[float] = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        all_headers = {"Accept": "application/json", **(headers or {}), **self._auth_header()}

        # Token destination pinning: a bearer token must go ONLY to this client's own HTTPS host --
        # never to a wrong/cross-tier/off-host destination (e.g. an unexpected redirect Location). A
        # bearer is reusable, broad access, so this fails closed. See safety.BearerDestinationError.
        if "Authorization" in all_headers:
            target = urlsplit(url)
            if target.scheme != "https" or target.netloc != self._host:
                logger.warning(
                    "Refused to send a bearer token to %s://%s (expected https://%s).",
                    target.scheme,
                    target.netloc,
                    self._host,
                )
                raise BearerDestinationError(
                    f"Refusing to send a bearer token to {target.scheme}://{target.netloc} "
                    f"(expected https://{self._host}). A token may only go to its own service host."
                )

        log_request(logger, method, url, params, json.dumps(json_body) if json_body is not None else None)

        resp = self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=all_headers,
            timeout=self._timeout if timeout is None else timeout,
        )

        log_response(logger, resp)
        resp.raise_for_status()
        return resp
