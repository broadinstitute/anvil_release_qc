"""Adapters that run the pyops-service-toolkit (``ops_utils``) classes under this repo's safety rules.

The toolkit's ``TDR`` and ``TerraWorkspace`` classes already implement the TDR / workspace calls this
project needs. Their ``RunRequest`` transport, however, has none of this repo's guarantees, so every
toolkit class here is handed a :class:`GuardedRunRequest` that restores them:

- **Bearer-destination pinning** (safety layer 4): the token is sent only to one HTTPS host (the tier's
  own service host from ``config.SERVICE_URLS``); anything else raises ``BearerDestinationError``.
- **Redacted request/response logging** (the shared ``logging_setup.log_request`` / ``log_response``).
- **A per-request timeout** and **no automatic retries** (the toolkit's default is unbounded timeout and
  exponential retry of every HTTP error, which would make a 403/404 preflight take minutes).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import urlsplit

import requests
from ops_utils.request_util import RunRequest
from ops_utils.tdr_utils.tdr_api_utils import TDR
from ops_utils.terra_util import TerraWorkspace

from anvil_release_qc.config import ResolvedTier
from anvil_release_qc.logging_setup import LOGGER_NAME, log_request, log_response
from anvil_release_qc.safety import BearerDestinationError

logger = logging.getLogger(LOGGER_NAME)


class _ProviderToken:
    """Minimal stand-in for the toolkit's ``Token`` (only what ``RunRequest.__init__`` stores)."""

    def __init__(self, provider: Callable[[], str]) -> None:
        self._provider = provider

    def get_token(self) -> str:
        return self._provider()


class GuardedRunRequest(RunRequest):
    """A ``RunRequest`` that pins the bearer token to one host, redacts its logs, and bounds requests."""

    def __init__(
        self, token_provider: Callable[[], str], pinned_base_url: str, timeout: float = 60
    ) -> None:
        super().__init__(_ProviderToken(token_provider), max_retries=1)
        self._token_provider = token_provider
        self._host = urlsplit(pinned_base_url).netloc
        self._timeout = timeout

    def create_headers(
        self, content_type: Optional[str] = None, accept: Optional[str] = "application/json"
    ) -> dict:
        headers: dict[str, str] = {}
        token = self._token_provider()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if accept:
            headers["accept"] = accept
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def run_request(  # type: ignore[override]
        self,
        uri: str,
        method: str,
        data: Any = None,
        files: Any = None,
        params: Optional[dict] = None,
        factor: int = 15,
        accept: Optional[str] = "application/json",
        content_type: Optional[str] = None,
        accept_return_codes: Optional[list[int]] = None,
    ) -> requests.Response:
        headers = self.create_headers(content_type=content_type, accept=accept)

        if "Authorization" in headers:
            target = urlsplit(uri)
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

        log_request(logger, method, uri, params, data)

        resp = requests.request(
            method,
            uri,
            headers=headers,
            params=params,
            data=data,
            files=files,
            timeout=self._timeout,
        )
        log_response(logger, resp)
        if not (200 <= resp.status_code < 300) and resp.status_code not in (
            accept_return_codes or []
        ):
            resp.raise_for_status()
        return resp


def _assert_same_host(toolkit_url: str, tier_url: str, what: str) -> None:
    """Fail loudly if the toolkit's hardcoded endpoint no longer matches this repo's tier config."""
    if urlsplit(toolkit_url).netloc != urlsplit(tier_url).netloc:
        raise ValueError(
            f"pyops-service-toolkit's {what} endpoint {toolkit_url!r} does not match this tier's "
            f"configured {tier_url!r}; refusing to send credentials there."
        )


def make_tdr(tier: ResolvedTier, token_provider: Callable[[], str], timeout: float = 60) -> TDR:
    """A toolkit ``TDR`` for ``tier``, with the bearer token pinned to the tier's TDR host."""
    tdr_url = tier.url("tdr")
    tdr = TDR(GuardedRunRequest(token_provider, tdr_url, timeout), env=tier.name)
    _assert_same_host(tdr.tdr_link, tdr_url, "TDR")
    return tdr


def make_terra_workspace(
    tier: ResolvedTier,
    token_provider: Callable[[], str],
    namespace: str,
    name: str,
    timeout: float = 180,
) -> TerraWorkspace:
    """A toolkit ``TerraWorkspace`` (Firecloud Orchestration), token pinned to the tier's host."""
    fc_url = tier.url("firecloud")
    workspace = TerraWorkspace(
        namespace, name, GuardedRunRequest(token_provider, fc_url, timeout), env=tier.name
    )
    _assert_same_host(workspace.terra_link, fc_url, "Terra (Firecloud Orchestration)")
    return workspace
