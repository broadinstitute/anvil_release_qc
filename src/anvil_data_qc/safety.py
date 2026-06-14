"""Safety controls for handling controlled-access data.

The Azul PFB signed URL grants direct access to **all of an NIH study's controlled-access tabular
data**. Mishandling it -- e.g. importing a production signed URL into the insecure dev tier, or any
unintended destination -- would be a Federal Data Management Incident (DMI). This pipeline is **NOT
yet approved** for controlled-access data; that approval requires a security-team review of the
end-to-end flow.

Until then, the pipeline is gated to **explicitly open-access snapshots only**: the TDR snapshot's
``consentCode`` must be exactly ``"NRES"`` (No Restrictions). The gate **fails closed** -- an empty,
missing, differently-cased, or any other consent code is treated as controlled/unknown and blocked.
It applies in **every tier** (defense in depth) and aborts **before any PFB signed URL is generated**.

Do not weaken this gate (e.g. by treating empty/unknown as open-access) without that security review.
"""

from __future__ import annotations

from typing import Optional

#: The TDR ``consentCode`` meaning "No Restrictions" -- the only value treated as open-access.
OPEN_ACCESS_CONSENT_CODE = "NRES"


class ControlledAccessNotApprovedError(RuntimeError):
    """Raised to block the pipeline on any snapshot that is not explicitly open-access."""


def is_open_access(consent_code: Optional[str]) -> bool:
    """True only when ``consent_code`` is exactly ``"NRES"``.

    Fails closed: empty string, ``None``, a different case, or any other value returns ``False``
    (controlled-access or unknown). Never infer open-access from a missing/empty consent code.
    """
    return consent_code == OPEN_ACCESS_CONSENT_CODE


def assert_open_access(snapshot_id: str, consent_code: Optional[str]) -> None:
    """Raise ``ControlledAccessNotApprovedError`` unless the snapshot is explicitly open-access."""
    if not is_open_access(consent_code):
        raise ControlledAccessNotApprovedError(
            f"Snapshot {snapshot_id} has consentCode={consent_code!r}, not "
            f"'{OPEN_ACCESS_CONSENT_CODE}' (open-access). This pipeline is NOT yet approved for "
            "running on controlled-access data -- the flow must be reviewed with the security team "
            "before any controlled-access use. Aborting before any PFB signed URL is generated."
        )
