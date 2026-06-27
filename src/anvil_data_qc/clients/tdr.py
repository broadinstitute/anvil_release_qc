"""TDR (Terra Data Repository) client.

Used by both QC flows:

- ``retrieve_snapshot`` -- the snapshot's name, ``consentCode``, and BigQuery access info (project +
  dataset) for the ``anvil_dataset`` query and the table comparison. Shared by ade-qc and tdr-qc.
- the **tdr-qc direct export** -- start an async snapshot export, poll the TDR job, read the result,
  and obtain the signed ``manifest.json`` URL the Terra import consumes. See docs/tdr_export_flow.md:

    GET /api/repository/v1/snapshots/{id}/export?exportGsPaths=false&validatePrimaryKeyUniqueness=true
      -> 202 {"id": <jobId>, "job_status": "running", ...}
    GET /api/repository/v1/jobs/{jobId}          (poll) -> job_status running/succeeded/failed
    GET /api/repository/v1/jobs/{jobId}/result   -> {..., "format": {"parquet": {"manifest": <signed gs url>}}}

``retrieve_snapshot``'s shape is from the TDR OpenAPI spec (confirmed on the dev open-access run); the
export flow is from real captured traffic (docs/tdr_export_flow.md), validated end-to-end on prod 1000G.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from ..logging_setup import LOGGER_NAME
from ..safety import SignedUrl
from .base import BaseClient

logger = logging.getLogger(LOGGER_NAME)

# Mirror the interactive UI's snapshot-export params (docs/tdr_export_flow.md), surfaced here so they
# are a single, easy place to flip. ``exportGsPaths=false`` yields signed https URLs (what the Terra
# import needs, vs gs:// paths); ``validatePrimaryKeyUniqueness=true`` validates primary-key uniqueness
# during export -- a non-unique PK FAILS the export, which is a legitimate QC finding (flip to False if
# it proves over-strict for some datasets).
EXPORT_GS_PATHS = False
VALIDATE_PRIMARY_KEY_UNIQUENESS = True

# TDR async-job terminal states (the ``job_status`` field; HTTP is 202 while running, 200 when done).
JOB_SUCCESS = "succeeded"
JOB_FAILURE = "failed"


class TdrClient(BaseClient):
    def retrieve_snapshot(self, snapshot_id: str) -> dict:
        """GET the snapshot with access information included."""
        return self.request(
            "GET",
            f"/api/repository/v1/snapshots/{snapshot_id}",
            params={"include": ["ACCESS_INFORMATION", "SOURCES", "PROPERTIES"]},
        ).json()

    def export_snapshot(
        self,
        snapshot_id: str,
        *,
        export_gs_paths: bool = EXPORT_GS_PATHS,
        validate_primary_key_uniqueness: bool = VALIDATE_PRIMARY_KEY_UNIQUENESS,
    ) -> dict:
        """Start an async snapshot export; returns the TDR job (poll with ``wait_for_export_job``)."""
        return self.request(
            "GET",
            f"/api/repository/v1/snapshots/{snapshot_id}/export",
            # requests renders Python bools as "True"/"False"; TDR expects lowercase.
            params={
                "exportGsPaths": str(export_gs_paths).lower(),
                "validatePrimaryKeyUniqueness": str(validate_primary_key_uniqueness).lower(),
            },
        ).json()

    def get_job(self, job_id: str) -> dict:
        """GET a TDR async job's status (``job_status``: running/succeeded/failed)."""
        return self.request("GET", f"/api/repository/v1/jobs/{job_id}").json()

    def get_job_result(self, job_id: str) -> dict:
        """GET a completed TDR job's result payload."""
        return self.request("GET", f"/api/repository/v1/jobs/{job_id}/result").json()

    def wait_for_export_job(
        self, job_id: str, poll_interval: float = 10.0, max_wait: float = 7200.0
    ) -> dict:
        """Poll a TDR export job until it succeeds; raise on failure or timeout.

        Large snapshots (the reason tdr-qc exists) can take a while to export, so ``max_wait`` matches
        the import budget (2h). Any non-terminal status -- including an unknown/missing one -- keeps
        polling (treated as still-in-progress) until a terminal state or the deadline.
        """
        deadline = time.monotonic() + max_wait
        while True:
            job = self.get_job(job_id)
            status = job.get("job_status")
            logger.info("TDR export job %s: job_status=%s", job_id, status)
            if status == JOB_SUCCESS:
                return job
            if status == JOB_FAILURE:
                # Surface job_id + status only (not the whole payload) -- the status payload has no
                # signed URLs, but keep the redaction discipline of the rest of the flow.
                raise RuntimeError(
                    f"TDR export job {job_id} failed (job_status={status!r}, "
                    f"status_code={job.get('status_code')!r})."
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"TDR export job {job_id} did not finish within {max_wait}s.")
            time.sleep(poll_interval)


def snapshot_consent_code(snapshot: dict) -> Optional[str]:
    """The snapshot's top-level ``consentCode``.

    ``"NRES"`` (No Restrictions) means open-access; anything else -- including empty or missing -- is
    controlled-access or unknown. Used by the safety gate (see ``anvil_data_qc.safety``).
    """
    return snapshot.get("consentCode")


def snapshot_name(snapshot: dict) -> str:
    name = snapshot.get("name")
    if not name:
        raise ValueError("Snapshot response is missing 'name'.")
    return name


def snapshot_bigquery_access(snapshot: dict) -> tuple[str, str]:
    """Return ``(bq_project, bq_dataset)`` from the snapshot's access information."""
    bq = (snapshot.get("accessInformation") or {}).get("bigQuery") or {}
    project = bq.get("projectId")
    dataset = bq.get("datasetName")
    if not project or not dataset:
        raise ValueError(
            "Snapshot is missing BigQuery access info (accessInformation.bigQuery.projectId/datasetName)."
        )
    return project, dataset


def export_job_id(export_response: dict) -> str:
    """Extract the TDR job id from a snapshot-export submit response (the ``id`` field)."""
    job_id = export_response.get("id")
    if not job_id:
        raise ValueError(f"Could not find a job id in the TDR export response: {export_response}")
    return job_id


def export_manifest_url(result: dict) -> SignedUrl:
    """Return the signed ``manifest.json`` URL from a TDR export result, wrapped for containment.

    The Terra import consumes ``format.parquet.manifest`` (a signed GCS URL to the export's
    ``manifest.json``). The result also carries per-table parquet signed URLs
    (``format.parquet.location.tables[].paths``) that this flow does not use. The manifest is wrapped
    in :class:`~anvil_data_qc.safety.SignedUrl` -- a redacting holder; unwrap it only at the import
    hand-off. Do not log the raw ``result`` (it is full of signed URLs).
    """
    parquet = ((result or {}).get("format") or {}).get("parquet") or {}
    manifest = parquet.get("manifest")
    if not manifest:
        raise ValueError(
            "TDR export result is missing format.parquet.manifest (the signed import manifest URL)."
        )
    return SignedUrl(manifest)
