"""Terra workspace naming.

Name = ``<user-local-part>_anvil_azul_<sanitized snapshot name>_YYYYMMDDHHMM`` (run start time,
minute-granular -- so batch reruns do not collide on an existing workspace). Any non-``[A-Za-z0-9_-]``
run is collapsed to a single ``_``. See CLAUDE.md -> "Settled build decisions".
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_NON_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize(text: str) -> str:
    """Collapse any run of disallowed characters to a single underscore; trim leading/trailing ``_``."""
    return _NON_NAME_CHARS.sub("_", text).strip("_")


def workspace_name(user_email: str, snapshot_name: str, when: Optional[datetime] = None) -> str:
    """Build the per-run workspace name for a snapshot export."""
    when = when or datetime.now()
    user_local_part = user_email.split("@", 1)[0]
    timestamp = when.strftime("%Y%m%d%H%M")
    return f"{sanitize(user_local_part)}_anvil_azul_{sanitize(snapshot_name)}_{timestamp}"
