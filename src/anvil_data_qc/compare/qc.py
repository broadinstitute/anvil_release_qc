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

from ..clients.rawls import RawlsClient, workspace_auth_domains
from ..clients.sam import SamClient
from ..config import ResolvedTier
from .authdomain import AuthDomainResult, check_auth_domain
from .engine import run_comparison
from .profile import ComparisonProfile
from .report import ComparisonReport
from .sources import BigQuerySnapshotSource, RawlsWorkspaceSource


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

    bq = bigquery.Client(project=tier.billing_project, credentials=creds)
    reference = BigQuerySnapshotSource(bq, bq_project, bq_dataset)
    rawls = RawlsClient(tier.url("rawls"), token_provider, timeout=180)
    candidate = RawlsWorkspaceSource(rawls, namespace, workspace_name)
    tables = run_comparison(reference, candidate, profile)

    sam = SamClient(tier.url("sam"), token_provider)
    expected = sam.snapshot_auth_domain(snapshot_id)
    actual = workspace_auth_domains(rawls.get_workspace(namespace, workspace_name))
    auth_domain = check_auth_domain(expected, actual)

    return QcComparisonResult(tables=tables, auth_domain=auth_domain)
