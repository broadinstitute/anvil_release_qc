"""Run the ingestion-coverage comparison for one submitter (ADSW) workspace vs one TDR snapshot.

This is the **ADSW -> TDR** step of the release pipeline -- the first and most fundamental QC: verify
that every table the submitter provided was ingested into the system of record (TDR) with the same row
count. It wires the Rawls (submitter workspace) and BigQuery (TDR snapshot) sources into the coverage
engine; used by the ``compare-ingest`` CLI command and, in batch, the Docker/WDL entrypoint.

Unlike the export/import QC, this flow does **no** PFB export -- no Azul, no signed URL, no consent
gate. It reads table/row counts via Rawls + BigQuery only.
"""

from __future__ import annotations

from typing import Callable

from google.auth.credentials import Credentials
from google.cloud import bigquery

from ..clients.rawls import RawlsClient
from ..config import ResolvedTier
from .engine import compare_coverage
from .report import CoverageReport
from .sources import BigQuerySnapshotSource, RawlsWorkspaceSource


def run_ingest_comparison(
    *,
    creds: Credentials,
    tier: ResolvedTier,
    bq_project: str,
    bq_dataset: str,
    submitter_namespace: str,
    submitter_workspace_name: str,
    token_provider: Callable[[], str],
) -> CoverageReport:
    """Compare a submitter workspace's tables to the TDR snapshot (table presence + equal row counts).

    The submitter workspace is the reference; the comparison is driven from its table set, so TDR's
    harmonized ``anvil_*`` / ``file_inventory`` / ``workspace_attributes`` tables are out of scope.
    """
    rawls = RawlsClient(tier.url("rawls"), token_provider, timeout=180)
    reference = RawlsWorkspaceSource(
        rawls,
        submitter_namespace,
        submitter_workspace_name,
        label=f"submitter workspace {submitter_namespace}/{submitter_workspace_name}",
    )

    bq = bigquery.Client(project=tier.billing_google_project_id, credentials=creds)
    candidate = BigQuerySnapshotSource(bq, bq_project, bq_dataset)

    return compare_coverage(
        reference.table_inventory(),
        candidate.table_inventory(),
        reference_label=reference.label,
        candidate_label=candidate.label,
    )
