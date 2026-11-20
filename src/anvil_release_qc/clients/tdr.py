"""TDR (Terra Data Repository) client.

Used by both QC flows:

- the toolkit's ``TDR.get_snapshot_info`` -- the snapshot's name, ``consentCode``, and BigQuery access info (project +
  dataset) for the ``anvil_dataset`` query and the table comparison. Shared by ade-qc and tdr-qc.
- the **tdr-qc direct export** -- start an async snapshot export, poll the TDR job, read the result,
  and obtain the signed ``manifest.json`` URL the Terra import consumes. See docs/tdr_export_flow.md:

    GET /api/repository/v1/snapshots/{id}/export?exportGsPaths=false&validatePrimaryKeyUniqueness=true
      -> 202 {"id": <jobId>, "job_status": "running", ...}
    GET /api/repository/v1/jobs/{jobId}          (poll) -> job_status running/succeeded/failed
    GET /api/repository/v1/jobs/{jobId}/result   -> {..., "format": {"parquet": {"manifest": <signed gs url>}}}

the snapshot response's shape is from the TDR OpenAPI spec (confirmed on the dev open-access run); the
export flow is from real captured traffic (docs/tdr_export_flow.md), validated end-to-end on prod 1000G.
"""

from __future__ import annotations

import logging
import time

from ops_utils.tdr_utils.tdr_api_utils import TDR

from anvil_release_qc.logging_setup import LOGGER_NAME
from anvil_release_qc.safety import SignedUrl

logger = logging.getLogger(LOGGER_NAME)

# Mirror the interactive UI's snapshot-export params (docs/tdr_export_flow.md), surfaced here so they
# are a single, easy place to flip (passed to the toolkit's ``TDR.export_snapshot``; ``signUrls`` keeps its default, true). ``exportGsPaths=false`` yields signed https URLs (what the Terra
# import needs, vs gs:// paths); ``validatePrimaryKeyUniqueness=true`` validates primary-key uniqueness
# during export -- a non-unique PK FAILS the export, which is a legitimate QC finding (flip to False if
# it proves over-strict for some datasets).
EXPORT_GS_PATHS = False
VALIDATE_PRIMARY_KEY_UNIQUENESS = True

# TDR async-job terminal states (the ``job_status`` field; HTTP is 202 while running, 200 when done).
JOB_SUCCESS = "succeeded"
JOB_FAILURE = "failed"


#: ``include`` sections passed to the toolkit's ``TDR.get_snapshot_info`` (access info -> BigQuery location).
SNAPSHOT_INCLUDE = ["ACCESS_INFORMATION", "SOURCES", "PROPERTIES"]


def wait_for_export_job(
    tdr: TDR, job_id: str, poll_interval: float = 10.0, max_wait: float = 7200.0
) -> dict:
    """Poll a TDR export job until it succeeds; raise on failure or timeout.

    Large snapshots (the reason tdr-qc exists) can take a while to export, so ``max_wait`` matches
    the import budget (2h). Any non-terminal status -- including an unknown/missing one -- keeps
    polling (treated as still-in-progress) until a terminal state or the deadline.
    """
    deadline = time.monotonic() + max_wait
    while True:
        job = tdr.get_job_status(job_id).json()
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

def export_manifest_url(result: dict) -> SignedUrl:
    """Return the signed ``manifest.json`` URL from a TDR export result, wrapped for containment.

    The Terra import consumes ``format.parquet.manifest`` (a signed GCS URL to the export's
    ``manifest.json``). The result also carries per-table parquet signed URLs
    (``format.parquet.location.tables[].paths``) that this flow does not use. The manifest is wrapped
    in :class:`~anvil_release_qc.safety.SignedUrl` -- a redacting holder; unwrap it only at the import
    hand-off. Do not log the raw ``result`` (it is full of signed URLs).
    """
    parquet = ((result or {}).get("format") or {}).get("parquet") or {}
    manifest = parquet.get("manifest")
    if not manifest:
        raise ValueError(
            "TDR export result is missing format.parquet.manifest (the signed import manifest URL)."
        )
    return SignedUrl(manifest)
