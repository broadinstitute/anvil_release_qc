"""Expected-difference handling for the comparison (config, not code).

Holds the knobs that encode how the candidate (AW) is *expected* to differ from the reference (TDR
snapshot), so the engine stays generic. Today: which tables to exclude from the strict 1:1 parity
check, split **by provenance** so the report can name each accurately (TDR-only system tables vs
import-added AW tables). Future seams attach here -- table/column name mapping, type-normalization
rules, and the deferred full column-name comparison (see docs/comparison.md).
"""

from __future__ import annotations

from dataclasses import dataclass

# TDR system tables present in every snapshot, intentionally never exported to the AW.
TDR_SYSTEM_TABLES = frozenset({"datarepo_row_ids"})

# AW entity types the PFB/ADE import synthesizes into the workspace, with no TDR-snapshot counterpart
# (their columns are all `pfb:`-prefixed). E.g. `duos_dataset_registration` -- the DUOS (Data Use
# Oversight System) registration record, present for DUOS-registered (i.e. real prod) datasets.
# Excluded so strict 1:1 table parity does not flag them as unexpected "AW-only" tables.
IMPORT_ADDED_AW_TABLES = frozenset({"duos_dataset_registration"})


@dataclass(frozen=True)
class ComparisonProfile:
    """Expected-difference configuration for one comparison.

    Excluded tables are split by provenance so the report names each accurately:

    - ``tdr_system_tables`` -- TDR-only internals present in the snapshot/BigQuery but never exported
      (e.g. ``datarepo_row_ids``).
    - ``import_added_aw_tables`` -- entity types the PFB/ADE import adds to the workspace with no
      TDR-snapshot counterpart (e.g. ``duos_dataset_registration``).
    """

    #: TDR-only system tables to exclude (TDR plumbing, not dataset content).
    tdr_system_tables: frozenset[str] = TDR_SYSTEM_TABLES
    #: AW-only import-added tables to exclude (synthesized by the import, no TDR counterpart).
    import_added_aw_tables: frozenset[str] = IMPORT_ADDED_AW_TABLES

    @property
    def ignore_tables(self) -> frozenset[str]:
        """All excluded table names, regardless of provenance -- used by the engine's set logic."""
        return self.tdr_system_tables | self.import_added_aw_tables
