"""Terra workspace naming.

Name = ``<user-local-part>_qc_anvil_ade_<sanitized snapshot name>_YYYYMMDDHHMM`` (run start time,
minute-granular -- so batch reruns do not collide on an existing workspace). Any non-``[A-Za-z0-9_-]``
run is collapsed to a single ``_``. See CLAUDE.md -> "Settled build decisions".

The ``qc`` segment leads so these workspaces are easy to identify, group, and eventually delete (they
accumulate per run -- risk R4). ``ade`` (AnVIL Data Explorer, the public-facing name) is preferred
over the backend service name ``azul``. The exact name may evolve as we learn what best supports
lifecycle management; ``_NAME_INFIX`` is the single point to change it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_NAME_INFIX = "qc_anvil_ade"


def sanitize(text: str) -> str:
    """Collapse any run of disallowed characters to a single underscore; trim leading/trailing ``_``."""
    return _NON_NAME_CHARS.sub("_", text).strip("_")


def workspace_name(user_email: str, snapshot_name: str, when: Optional[datetime] = None) -> str:
    """Build the per-run workspace name for a snapshot export."""
    when = when or datetime.now()
    user_local_part = user_email.split("@", 1)[0]
    timestamp = when.strftime("%Y%m%d%H%M")
    return f"{sanitize(user_local_part)}_{_NAME_INFIX}_{sanitize(snapshot_name)}_{timestamp}"
