"""Structured result of a table comparison, with a human-readable render for the log / CLI.

The comparison report is an essential output of every batch run, so it is structured (for programmatic
checks) and renders to a clear, scannable block (for the WDL log and CLI).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _n(value: Optional[int]) -> str:
    return "-" if value is None else str(value)


@dataclass(frozen=True)
class TableComparison:
    """One table's result: presence on each side, row counts, and column counts."""

    name: str
    in_reference: bool
    in_candidate: bool
    reference_rows: Optional[int]
    candidate_rows: Optional[int]
    reference_nonempty_columns: Optional[int]
    candidate_columns: Optional[int]
    row_ok: bool
    column_ok: bool

    @property
    def passed(self) -> bool:
        return self.in_reference and self.in_candidate and self.row_ok and self.column_ok

    def status_label(self) -> str:
        if not self.in_candidate:
            return "MISSING from AW (data lost)"
        if not self.in_reference:
            return "AW-ONLY (investigate)"
        issues = []
        if not self.row_ok:
            issues.append("row-count mismatch")
        if not self.column_ok:
            issues.append("column shortfall (AW < TDR non-empty)")
        return ", ".join(issues) if issues else "OK"


@dataclass(frozen=True)
class ComparisonReport:
    """The full table-comparison result for one snapshot vs one workspace."""

    reference_label: str
    candidate_label: str
    tables: list[TableComparison]
    empty_reference_tables: list[str]  # 0-row TDR tables, excluded (expected import drop)
    tdr_system_tables: list[str]  # TDR-only internals, excluded (e.g. datarepo_row_ids)
    # AW-only import artifacts, excluded (e.g. duos_dataset_registration)
    import_added_aw_tables: list[str]

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.tables)

    def failures(self) -> list[TableComparison]:
        return [t for t in self.tables if not t.passed]

    def render(self) -> str:
        lines = [
            f"Table comparison — {'PASS' if self.passed else 'FAIL'} "
            f"({len(self.tables)} tables compared, {len(self.failures())} failing)",
            f"  reference (TDR): {self.reference_label}",
            f"  candidate (AW):  {self.candidate_label}",
        ]
        for t in self.tables:
            mark = "OK " if t.passed else "XX "
            lines.append(
                f"    {mark} {t.name:<26} "
                f"rows {_n(t.reference_rows):>8} -> {_n(t.candidate_rows):<8} "
                f"cols(ne) {_n(t.reference_nonempty_columns):>4} -> {_n(t.candidate_columns):<4}  "
                f"{t.status_label()}"
            )
        if self.empty_reference_tables:
            lines.append(
                f"  excluded — empty TDR tables (expected drop): {self.empty_reference_tables}"
            )
        if self.tdr_system_tables:
            lines.append(
                f"  excluded — TDR system tables (TDR-only internals): {self.tdr_system_tables}"
            )
        if self.import_added_aw_tables:
            lines.append(
                f"  excluded — import-added AW tables (no TDR counterpart): {self.import_added_aw_tables}"
            )
        return "\n".join(lines)
