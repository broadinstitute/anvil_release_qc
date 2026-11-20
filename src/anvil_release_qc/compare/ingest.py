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

from anvil_release_qc.clients.toolkit import make_terra_workspace
from anvil_release_qc.compare.engine import compare_coverage
from anvil_release_qc.compare.report import CoverageReport
from anvil_release_qc.compare.sources import BigQuerySnapshotSource, RawlsWorkspaceSource
from anvil_release_qc.config import ResolvedTier


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
    workspace = make_terra_workspace(
        tier, token_provider, submitter_namespace, submitter_workspace_name
    )
    reference = RawlsWorkspaceSource(
        workspace,
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
