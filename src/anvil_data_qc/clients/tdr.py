"""TDR (Terra Data Repository) client.

Step 1 of the flow: retrieve the snapshot to get its name and BigQuery access information (project +
dataset) for the ``anvil_dataset`` query. See docs/export_import_flow.md section 1.

NOTE: the export captures did not include a TDR call, so the endpoint/response shape here is based on
the TDR OpenAPI spec (docs/services.md) and should be confirmed against a real call during the first
dev-tier run.
"""

from __future__ import annotations

from .base import BaseClient


class TdrClient(BaseClient):
    def retrieve_snapshot(self, snapshot_id: str) -> dict:
        """GET the snapshot with access information included."""
        return self.request(
            "GET",
            f"/api/repository/v1/snapshots/{snapshot_id}",
            params={"include": ["ACCESS_INFORMATION", "SOURCES", "PROPERTIES"]},
        ).json()


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
