"""The comparison engine: compare two table inventories into a ComparisonReport.

T1-T2 rules (counts only; full column-name and cell-value comparison are deferred -- see
docs/comparison.md):

- Build the TDR baseline on **non-empty, non-ignored** tables (Terra drops empty source tables/columns
  on import, and TDR system tables are excluded by the profile).
- Strict 1:1: every baseline TDR table must have a same-named AW table, and every AW table must have a
  same-named baseline TDR table. A *missing* table (data lost) or an *AW-only* table both **fail**.
- For matched tables: row counts must be equal, and the AW column count must be >= the TDR non-empty
  column count (columns may be added by Terra, never dropped).
"""

from __future__ import annotations

from .profile import ComparisonProfile
from .report import ComparisonReport, TableComparison
from .sources import TableShape, TabularDataSource


def compare_inventories(
    reference: dict[str, TableShape],
    candidate: dict[str, TableShape],
    profile: ComparisonProfile,
    *,
    reference_label: str,
    candidate_label: str,
) -> ComparisonReport:
    """Compare two ``{table: TableShape}`` inventories under ``profile``."""
    ignore = profile.ignore_tables
    ref = {t: s for t, s in reference.items() if t not in ignore}
    cand = {t: s for t, s in candidate.items() if t not in ignore}

    empty_reference = sorted(t for t, s in ref.items() if s.row_count == 0)
    baseline = {t: s for t, s in ref.items() if s.row_count > 0}  # non-empty TDR tables

    tables: list[TableComparison] = []
    for name in sorted(set(baseline) | set(cand)):
        r = baseline.get(name)
        c = cand.get(name)
        in_ref, in_cand = r is not None, c is not None
        row_ok = bool(in_ref and in_cand and r.row_count == c.row_count)
        column_ok = bool(in_ref and in_cand and c.total_columns >= r.nonempty_columns)
        tables.append(
            TableComparison(
                name=name,
                in_reference=in_ref,
                in_candidate=in_cand,
                reference_rows=r.row_count if r else None,
                candidate_rows=c.row_count if c else None,
                reference_nonempty_columns=r.nonempty_columns if r else None,
                candidate_columns=c.total_columns if c else None,
                row_ok=row_ok,
                column_ok=column_ok,
            )
        )

    present = set(reference) | set(candidate)
    return ComparisonReport(
        reference_label=reference_label,
        candidate_label=candidate_label,
        tables=tables,
        empty_reference_tables=empty_reference,
        tdr_system_tables=sorted(present & profile.tdr_system_tables),
        import_added_aw_tables=sorted(present & profile.import_added_aw_tables),
    )


def run_comparison(
    reference: TabularDataSource, candidate: TabularDataSource, profile: ComparisonProfile
) -> ComparisonReport:
    """Fetch both inventories and compare them."""
    return compare_inventories(
        reference.table_inventory(),
        candidate.table_inventory(),
        profile,
        reference_label=reference.label,
        candidate_label=candidate.label,
    )
