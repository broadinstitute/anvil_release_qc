"""Terra workspace naming.

Name = ``<user-local-part>_<infix>_<sanitized snapshot name>_YYYYMMDDHHMM`` (run start time,
minute-granular -- so batch reruns do not collide on an existing workspace). Any non-``[A-Za-z0-9_-]``
run is collapsed to a single ``_``. See CLAUDE.md -> "Settled build decisions".

The ``qc`` segment leads so these workspaces are easy to identify, group, and eventually delete (they
accumulate per run -- risk R4). The infix also names the **export flow** that produced the workspace:
``ADE_NAME_INFIX`` (``qc_anvil_ade`` -- AnVIL Data Explorer, the public-facing name, preferred over the
backend service name ``azul``) for the ADE/Azul flow, ``TDR_NAME_INFIX`` (``qc_anvil_tdr``) for the TDR
direct-export flow. The exact names may evolve as we learn what best supports lifecycle management;
the infix is a parameter so each flow selects its own.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")

#: Workspace-name infix per export flow (the leading ``qc`` keeps them grouped/identifiable).
ADE_NAME_INFIX = "qc_anvil_ade"
TDR_NAME_INFIX = "qc_anvil_tdr"


def sanitize(text: str) -> str:
    """Collapse any run of disallowed characters to a single underscore; trim leading/trailing ``_``."""
    return _NON_NAME_CHARS.sub("_", text).strip("_")


def workspace_name(
    user_email: str,
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
    user_local_part = user_email.split("@", 1)[0]
    timestamp = when.strftime("%Y%m%d%H%M")
    return f"{sanitize(user_local_part)}_{infix}_{sanitize(snapshot_name)}_{timestamp}"
