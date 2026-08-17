"""Terra workspace client for the batch runner: method configs, submissions, entities, attributes.

Built on ``clients.base.BaseClient`` so it inherits the repo's redacted request/response logging and
bearer host-pinning. Scoped to one workspace (``namespace``/``name``) via Firecloud Orchestration
(the same service the pipelines use for ``importJob``). Endpoints + the ``SubmissionRequest`` schema were
confirmed against the firecloud-orchestration swagger; see docs / the plan.

  GET  /api/workspaces/{ns}/{name}/methodconfigs?allRepos=true        -> [ {name, namespace, rootEntityType, ...} ]
  POST /api/workspaces/{ns}/{name}/submissions                        -> { submissionId, ... }
  GET  /api/workspaces/{ns}/{name}/submissions/{submissionId}         -> { status, workflows:[{status,...}], ... }
  GET  /api/workspaces/{ns}/{name}/entities/{entityType}/{entityName} -> { name, attributes:{...}, ... }
  GET  /api/workspaces/{ns}/{name}?fields=workspace.attributes        -> { workspace:{attributes:{...}} }
"""

from __future__ import annotations

from typing import Optional

from ..clients.base import BaseClient

# Submission-level status (SubmissionStatus): terminal states end a submission.
SUBMISSION_TERMINAL = frozenset({"Done", "Aborted"})
# Workflow-level status (WorkflowStatus) terminal states.
WORKFLOW_SUCCEEDED = "Succeeded"
WORKFLOW_FAILED = "Failed"
WORKFLOW_ABORTED = "Aborted"


class TerraWorkspaceClient(BaseClient):
    """Firecloud-Orchestration calls scoped to a single workspace (``namespace``/``name``)."""

    def __init__(self, base_url, token_provider, namespace, name, timeout: int = 180) -> None:
        super().__init__(base_url, token_provider, timeout)
        self.namespace = namespace
        self.name = name

    def _ws(self, suffix: str) -> str:
        return f"/api/workspaces/{self.namespace}/{self.name}{suffix}"

    def list_method_configs(self) -> list[dict]:
        """List the workspace's method configurations (short form: name/namespace/rootEntityType/...)."""
        return self.request("GET", self._ws("/methodconfigs"), params={"allRepos": "true"}).json()

    def find_method_config(self, name: str) -> Optional[dict]:
        """Return the method config whose ``name`` matches, or ``None`` (used to resolve namespace)."""
        for cfg in self.list_method_configs():
            if cfg.get("name") == name:
                return cfg
        return None

    def create_submission(
        self,
        *,
        method_config_namespace: str,
        method_config_name: str,
        entity_type: str,
        entity_name: str,
        use_call_cache: bool = False,
        user_comment: Optional[str] = None,
    ) -> dict:
        """Launch a single-entity submission of a method config. Returns the submit response (submissionId)."""
        body: dict = {
            "methodConfigurationNamespace": method_config_namespace,
            "methodConfigurationName": method_config_name,
            "entityType": entity_type,
            "entityName": entity_name,
            "useCallCache": use_call_cache,
        }
        if user_comment:
            body["userComment"] = user_comment
        return self.request("POST", self._ws("/submissions"), json_body=body).json()

    def get_submission(self, submission_id: str) -> dict:
        """Monitor a submission: returns its status + per-workflow statuses."""
        return self.request("GET", self._ws(f"/submissions/{submission_id}")).json()

    def get_entity(self, entity_type: str, entity_name: str) -> dict:
        """Return one data-table row: ``{name, entityType, attributes: {...}}``."""
        return self.request("GET", self._ws(f"/entities/{entity_type}/{entity_name}")).json()

    def get_workspace_attributes(self) -> dict:
        """Return the workspace's attributes (Workspace Data key/values), e.g. the Azul-token gs:// path."""
        resp = self.request("GET", self._ws(""), params={"fields": "workspace.attributes"}).json()
        return (resp.get("workspace") or resp).get("attributes") or {}


# --- response extractors (kept as functions so they're easy to unit-test) ---


def submission_id(submit_response: dict) -> str:
    """Extract the submission id from a create-submission response."""
    sid = (submit_response or {}).get("submissionId") or (submit_response or {}).get("id")
    if not sid:
        raise ValueError(f"No submissionId in submit response: {submit_response}")
    return sid


def submission_status(submission: dict) -> Optional[str]:
    """The submission-level status (``Accepted``…``Done``/``Aborted``)."""
    return (submission or {}).get("status")


def is_submission_terminal(submission: dict) -> bool:
    """True once the submission has finished (``Done`` or ``Aborted``)."""
    return submission_status(submission) in SUBMISSION_TERMINAL


def workflow_statuses(submission: dict) -> list[str]:
    """Per-workflow statuses within a submission (usually one, for single-entity submissions)."""
    return [w.get("status") for w in (submission or {}).get("workflows") or [] if w.get("status")]
