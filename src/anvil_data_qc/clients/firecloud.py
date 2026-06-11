"""Firecloud Orchestration client.

Step 5 of the flow: import the PFB into the workspace and poll the async job to completion.
See docs/export_import_flow.md section 5.

  POST /api/workspaces/<ns>/<name>/importJob   {"url": <signed S3 URL>, "filetype": "pfb", "options": null}  -> 202
  GET  /api/workspaces/<ns>/<name>/importJob/<jobId>  (poll) -> terminal status

NOTE: the capture did not include the importJob response bodies, so the job-id field and status
values below are based on the Terra import-service API and must be confirmed on the first dev run.
"""

from __future__ import annotations

import logging
import time

from ..logging_setup import LOGGER_NAME
from .base import BaseClient

logger = logging.getLogger(LOGGER_NAME)

# Inferred -- confirm against a real importJob response on the first dev-tier run.
JOB_ID_FIELDS = ("jobId", "id")
SUCCESS_STATUS = "Done"
TERMINAL_STATUSES = frozenset({"Done", "Error", "Cancelled"})


class FirecloudClient(BaseClient):
    def submit_import_job(self, namespace: str, name: str, signed_url: str) -> dict:
        body = {"url": signed_url, "filetype": "pfb", "options": None}
        return self.request(
            "POST", f"/api/workspaces/{namespace}/{name}/importJob", json_body=body
        ).json()

    def get_import_job(self, namespace: str, name: str, job_id: str) -> dict:
        return self.request(
            "GET", f"/api/workspaces/{namespace}/{name}/importJob/{job_id}"
        ).json()

    def wait_for_import(
        self,
        namespace: str,
        name: str,
        job_id: str,
        poll_interval: float = 15.0,
        max_wait: float = 3600.0,
    ) -> dict:
        """Poll until the import job reaches a terminal state; raise unless it succeeded."""
        deadline = time.monotonic() + max_wait
        while True:
            job = self.get_import_job(namespace, name, job_id)
            status = job.get("status")
            logger.info("Import job %s: status=%s", job_id, status)
            if status in TERMINAL_STATUSES:
                if status != SUCCESS_STATUS:
                    raise RuntimeError(f"Import job {job_id} ended with status '{status}': {job}")
                return job
            if time.monotonic() > deadline:
                raise TimeoutError(f"Import job {job_id} did not finish within {max_wait}s.")
            time.sleep(poll_interval)


def import_job_id(submit_response: dict) -> str:
    """Extract the import job id from the submit response."""
    for field in JOB_ID_FIELDS:
        if submit_response.get(field):
            return submit_response[field]
    raise ValueError(f"Could not find an import job id in the submit response: {submit_response}")
