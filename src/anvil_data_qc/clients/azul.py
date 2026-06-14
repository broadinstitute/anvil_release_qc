"""AnVIL Data Explorer (Azul) client.

Step 2 of the flow: generate the verbatim PFB manifest for a dataset and poll until the signed S3 URL
is ready. See docs/export_import_flow.md section 2.

  PUT /fetch/manifest/files?catalog=<catalog>&filters={"datasets.dataset_id":{"is":[<id>]}}&format=verbatim.pfb
    -> {"Status": 301, "Location": ".../fetch/manifest/files/<token>", "Retry-After": <sec>}
  GET <Location> (poll while Status == 301) -> Status 302 with the signed S3 URL in "Location".
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from ..logging_setup import LOGGER_NAME
from ..safety import SignedUrl
from .base import BaseClient

logger = logging.getLogger(LOGGER_NAME)

PFB_FORMAT = "verbatim.pfb"


class AzulClient(BaseClient):
    def __init__(
        self,
        base_url: str,
        token_provider: Callable[[], str],
        catalog: str | None = None,
        timeout: int = 60,
    ) -> None:
        super().__init__(base_url, token_provider, timeout)
        self._catalog = catalog

    def default_catalog(self) -> str:
        """Discover the current default catalog (it is versioned, e.g. ``anvil14`` -> ``anvil15``)."""
        if self._catalog is None:
            self._catalog = self.request("GET", "/index/catalogs").json()["default_catalog"]
        return self._catalog

    def request_pfb_manifest(self, dataset_id: str, max_wait: float = 900.0) -> SignedUrl:
        """Submit the PFB manifest job for ``dataset_id`` and poll until the signed URL is returned.

        The result is wrapped in :class:`~anvil_data_qc.safety.SignedUrl` -- a redacting holder for the
        sensitive controlled-access credential; unwrap it only at the import hand-off.
        """
        filters = json.dumps({"datasets.dataset_id": {"is": [dataset_id]}})
        params = {"catalog": self.default_catalog(), "filters": filters, "format": PFB_FORMAT}
        body = self.request("PUT", "/fetch/manifest/files", params=params).json()
        return SignedUrl(self._poll(body, max_wait))

    def _poll(self, body: dict, max_wait: float) -> str:
        deadline = time.monotonic() + max_wait
        while True:
            status = body.get("Status")
            location = body.get("Location")
            if status == 302:
                logger.info("Azul manifest ready (signed URL obtained).")
                return location
            if status != 301:
                raise RuntimeError(f"Unexpected Azul manifest Status {status}: {body}")
            retry_after = float(body.get("Retry-After", 1))
            if time.monotonic() + retry_after > deadline:
                raise TimeoutError("Azul manifest generation timed out.")
            time.sleep(retry_after)
            body = self.request("GET", location).json()
