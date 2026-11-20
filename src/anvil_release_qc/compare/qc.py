"""Run the QC comparison for one TDR snapshot vs one Analysis Workspace.

Wires the BigQuery (TDR) and Rawls (AW) sources into the engine, and adds the auth-domain check, into
a single ``QcComparisonResult``. Used by the pipeline (post-import) and the standalone ``compare`` CLI
command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from google.auth.credentials import Credentials
from google.cloud import bigquery

from anvil_release_qc.clients.rawls import workspace_auth_domains
from anvil_release_qc.clients.sam import SamClient
from anvil_release_qc.clients.toolkit import make_terra_workspace
from anvil_release_qc.compare.authdomain import AuthDomainResult, check_auth_domain
from anvil_release_qc.compare.engine import run_comparison
from anvil_release_qc.compare.profile import ComparisonProfile
from anvil_release_qc.compare.report import ComparisonReport
from anvil_release_qc.compare.sources import BigQuerySnapshotSource, RawlsWorkspaceSource
from anvil_release_qc.config import ResolvedTier


@dataclass(frozen=True)
class QcComparisonResult:
    tables: ComparisonReport
    auth_domain: AuthDomainResult

    @property
    def passed(self) -> bool:
        return self.tables.passed and self.auth_domain.passed

    def render(self) -> str:
        return (
            f"{self.tables.render()}\n\n{self.auth_domain.render()}\n\n"
            f"Overall QC — {'PASS' if self.passed else 'FAIL'}"
        )


def run_qc_comparison(
    *,
    creds: Credentials,
    tier: ResolvedTier,
    snapshot_id: str,
    bq_project: str,
    bq_dataset: str,
    namespace: str,
    workspace_name: str,
    token_provider: Callable[[], str],
    profile: Optional[ComparisonProfile] = None,
) -> QcComparisonResult:
    """Compare the AW data tables to the TDR snapshot (T1-T2) and verify the auth domain."""
    profile = profile or ComparisonProfile()

    bq = bigquery.Client(project=tier.billing_google_project_id, credentials=creds)
    reference = BigQuerySnapshotSource(bq, bq_project, bq_dataset)
    workspace = make_terra_workspace(tier, token_provider, namespace, workspace_name)
    candidate = RawlsWorkspaceSource(workspace)
    tables = run_comparison(reference, candidate, profile)

    sam = SamClient(tier.url("sam"), token_provider)
    expected = sam.snapshot_auth_domain(snapshot_id)
    actual = workspace_auth_domains(workspace.get_workspace_info().json())
    auth_domain = check_auth_domain(expected, actual)

    return QcComparisonResult(tables=tables, auth_domain=auth_domain)
