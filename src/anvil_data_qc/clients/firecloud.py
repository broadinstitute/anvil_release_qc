"""Firecloud Orchestration client.

Step 5 of the flow: import the PFB into the workspace and poll the async job to completion.
See docs/export_import_flow.md section 5.

  POST /api/workspaces/<ns>/<name>/importJob   {"url": <signed S3 URL>, "filetype": "pfb", "options": null}  -> 202
  GET  /api/workspaces/<ns>/<name>/importJob/<jobId>  (poll) -> terminal status

Confirmed against a real dev import (2026-06-11): submit returns ``{"jobId", "url"}``; the status
endpoint returns ``{"filetype", "jobId", "status"}`` (see the status constants below).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..logging_setup import LOGGER_NAME
from ..safety import SignedUrl
from .base import BaseClient

logger = logging.getLogger(LOGGER_NAME)

JOB_ID_FIELDS = ("jobId", "id")  # the submit response uses "jobId"
SUCCESS_STATUS = "Done"
FAILURE_STATUSES = frozenset({"Error"})
# In-progress statuses keep polling: Pending, Translating, ReadyForUpsert, Upserting (+ any unknown).


class FirecloudClient(BaseClient):
    def submit_import_job(
        self,
        namespace: str,
        name: str,
        signed_url: SignedUrl,
        *,
        filetype: str = "pfb",
        options: Optional[dict] = None,
    ) -> dict:
        # THE SINGLE SITE that unwraps the signed URL: it goes straight into the importJob body and
        # nowhere else. The destination/provenance checks (safety.verify_pfb_handoff) run before this,
        # and the request body is logged redacted (base client). Do not add other reveal() calls.
        # filetype/options vary by flow: ade-qc -> ("pfb", None); tdr-qc -> ("tdrexport",
        # {"tdrSyncPermissions": ...}). The import endpoint and polling are identical for both.
        body = {"url": signed_url.reveal(), "filetype": filetype, "options": options}
        return self.request(
            "POST", f"/api/workspaces/{namespace}/{name}/importJob", json_body=body
        ).json()

    def get_import_job(self, namespace: str, name: str, job_id: str) -> dict:
        return self.request("GET", f"/api/workspaces/{namespace}/{name}/importJob/{job_id}").json()

    def wait_for_import(
        self,
        namespace: str,
        name: str,
        job_id: str,
        poll_interval: float = 30.0,
        max_wait: float = 7200.0,
    ) -> dict:
        """Poll until the import job succeeds ('Done'); raise on failure ('Error') or timeout.

        PFB imports can run a long time (~1 hour, sometimes more), hence the generous ``max_wait``.
        Any non-terminal status (Pending/Translating/ReadyForUpsert/Upserting/unknown) keeps polling.
        """
        deadline = time.monotonic() + max_wait
        while True:
            job = self.get_import_job(namespace, name, job_id)
            status = job.get("status")
            logger.info("Import job %s: status=%s", job_id, status)
            if status == SUCCESS_STATUS:
                return job
            if status in FAILURE_STATUSES:
                raise RuntimeError(f"Import job {job_id} failed with status '{status}': {job}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Import job {job_id} did not finish within {max_wait}s.")
            time.sleep(poll_interval)


def import_job_id(submit_response: dict) -> str:
    """Extract the import job id from the submit response."""
    for field in JOB_ID_FIELDS:
        if submit_response.get(field):
            return submit_response[field]
    raise ValueError(f"Could not find an import job id in the submit response: {submit_response}")
