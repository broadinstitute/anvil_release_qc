"""Terra workspace naming.

Name = ``ops-validation-<infix>_<sanitized snapshot name>_YYYYMMDDHHMM`` (run start time,
minute-granular -- so batch reruns do not collide on an existing workspace). Any non-``[A-Za-z0-9_-]``
run in the snapshot name is collapsed to a single ``_``. See CLAUDE.md -> "Settled build decisions".

Every name starts with the fixed ``WORKSPACE_NAME_PREFIX`` (``ops-validation-``) rather than the
operator's username, so these workspaces are easy to identify, group, and eventually delete regardless
of who ran them (they accumulate per run -- risk R4). The infix names the **export flow** that produced
the workspace: ``ADE_NAME_INFIX`` (``qc_anvil_ade`` -- AnVIL Data Explorer, the public-facing name,
preferred over the backend service name ``azul``) for the ADE/Azul flow, ``TDR_NAME_INFIX``
(``qc_anvil_tdr``) for the TDR direct-export flow. The infix is a parameter so each flow selects its own.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")

#: Fixed leading segment of every QC workspace name (identifies them independent of the operator).
WORKSPACE_NAME_PREFIX = "ops-validation-"

#: Workspace-name infix per export flow.
ADE_NAME_INFIX = "qc_anvil_ade"
TDR_NAME_INFIX = "qc_anvil_tdr"


def sanitize(text: str) -> str:
    """Collapse any run of disallowed characters to a single underscore; trim leading/trailing ``_``."""
    return _NON_NAME_CHARS.sub("_", text).strip("_")


def workspace_name(
    snapshot_name: str,
    when: Optional[datetime] = None,
    *,
    infix: str = ADE_NAME_INFIX,
) -> str:
    """Build the per-run workspace name for a snapshot export.

    ``infix`` names the export flow (``ADE_NAME_INFIX`` / ``TDR_NAME_INFIX``); it defaults to the
    ADE/Azul flow for backward compatibility.
    """
    when = when or datetime.now()
    timestamp = when.strftime("%Y%m%d%H%M")
    return f"{WORKSPACE_NAME_PREFIX}{infix}_{sanitize(snapshot_name)}_{timestamp}"
