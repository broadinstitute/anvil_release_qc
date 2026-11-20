"""Rawls client.

Step 4 of the flow: create the destination workspace (with an empty ``authorizationDomain`` -- the
import applies it). Also extracts a workspace's auth domain(s) from a workspace response (read via the toolkit).
See docs/export_import_flow.md sections 4 and 6.
"""

from __future__ import annotations

from typing import Optional

from anvil_release_qc.clients.base import BaseClient


class RawlsClient(BaseClient):
    def create_workspace(
        self,
        namespace: str,
        name: str,
        *,
        description: str = "",
        auth_domain: Optional[list[dict]] = None,
        bucket_location: str = "US-CENTRAL1",
        enhanced_bucket_logging: bool = True,
    ) -> dict:
        """POST /api/workspaces. ``authorizationDomain`` defaults to [] (the import applies it)."""
        body = {
            "namespace": namespace,
            "name": name,
            "authorizationDomain": list(auth_domain or []),
            "attributes": {"description": description},
            "copyFilesWithPrefix": "notebooks/",
            "bucketLocation": bucket_location,
            "enhancedBucketLogging": enhanced_bucket_logging,
            "addUsers": [],
        }
        return self.request("POST", "/api/workspaces", json_body=body).json()


def workspace_auth_domains(workspace: dict) -> list[str]:
    """Extract the auth-domain group names from a workspace response (for QC verification)."""
    ws = workspace.get("workspace", workspace)
    return [g["membersGroupName"] for g in (ws.get("authorizationDomain") or [])]
