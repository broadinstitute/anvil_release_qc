"""Expected-difference handling for the comparison (config, not code).

Holds the knobs that encode how the candidate (AW) is *expected* to differ from the reference (TDR
snapshot), so the engine stays generic. Today: which reference tables to ignore (TDR system tables).
Future seams attach here -- table/column name mapping, type-normalization rules, and the deferred
full column-name comparison (see docs/comparison.md).
"""

from __future__ import annotations

from dataclasses import dataclass

# TDR system tables present in every snapshot, intentionally never exported to the AW.
TDR_SYSTEM_TABLES = frozenset({"datarepo_row_ids"})


@dataclass(frozen=True)
class ComparisonProfile:
    """Expected-difference configuration for one comparison."""

    #: Reference table names to exclude from the comparison (TDR plumbing, not dataset content).
    ignore_tables: frozenset[str] = TDR_SYSTEM_TABLES
