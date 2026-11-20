"""Authorization-domain verification.

For controlled-access datasets the workspace's auth domain(s) must match the source snapshot's. The
import applies the auth domain (a privileged op); we only **verify** it. An empty or mismatched
workspace auth domain on a controlled dataset is a QC failure. (Open-access datasets have an empty
auth domain on both sides -- a trivial, correct match.)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthDomainResult:
    expected: list[str]  # from the TDR snapshot (Sam)
    actual: list[str]  # from the workspace (Rawls)

    @property
    def passed(self) -> bool:
        return set(self.expected) == set(self.actual)

    def render(self) -> str:
        return (
            f"Auth-domain check — {'PASS' if self.passed else 'FAIL'}\n"
            f"  snapshot (expected): {sorted(self.expected)}\n"
            f"  workspace (actual):  {sorted(self.actual)}"
        )


def check_auth_domain(expected: list[str], actual: list[str]) -> AuthDomainResult:
    """Compare the snapshot's auth-domain groups to the workspace's (order-insensitive)."""
    return AuthDomainResult(expected=list(expected), actual=list(actual))
