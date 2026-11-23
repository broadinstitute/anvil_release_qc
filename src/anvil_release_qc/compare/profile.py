"""Expected-difference handling for the comparison (config, not code).

Holds the knobs that encode how the candidate (AW) is *expected* to differ from the reference (TDR
snapshot), so the engine stays generic. Today: which tables to exclude from the strict 1:1 parity
check, split **by provenance** so the report can name each accurately (TDR-only system tables vs
import-added AW tables vs never-imported TDR tables), plus the row-count ceiling above which a TDR
table is too large to import and so is skipped. Future seams attach here -- table/column name mapping,
type-normalization rules, and the deferred full column-name comparison (see docs/comparison.md).
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

# TDR tables that are part of the snapshot but are never imported into the AW, so there is nothing to
# compare. ``file_inventory`` is the TDR file manifest -- not carried into the workspace data tables.
NOT_IMPORTED_TABLES = frozenset({"file_inventory"})

# Terra's data-table import cannot handle a table this large: any TDR table with MORE than this many
# rows is skipped (not compared, not a failure) and named in the report. See risk R4.
MAX_IMPORTABLE_TABLE_ROWS = 350_000


@dataclass(frozen=True)
class ComparisonProfile:
    """Expected-difference configuration for one comparison.

    Excluded tables are split by provenance so the report names each accurately:

    - ``tdr_system_tables`` -- TDR-only internals present in the snapshot/BigQuery but never exported
      (e.g. ``datarepo_row_ids``).
    - ``import_added_aw_tables`` -- entity types the PFB/ADE import adds to the workspace with no
      TDR-snapshot counterpart (e.g. ``duos_dataset_registration``).
    - ``not_imported_tables`` -- TDR tables that are never imported into the AW (e.g.
      ``file_inventory``); skipped and reported, never a failure.

    Separately, any TDR table with more than ``max_importable_table_rows`` rows is skipped (too large
    for Terra to import) and reported, never a failure.
    """

    #: TDR-only system tables to exclude (TDR plumbing, not dataset content).
    tdr_system_tables: frozenset[str] = TDR_SYSTEM_TABLES
    #: AW-only import-added tables to exclude (synthesized by the import, no TDR counterpart).
    import_added_aw_tables: frozenset[str] = IMPORT_ADDED_AW_TABLES
    #: TDR tables never imported into the AW (skipped, noted in the report).
    not_imported_tables: frozenset[str] = NOT_IMPORTED_TABLES
    #: TDR tables with more rows than this are too large to import (skipped, noted in the report).
    max_importable_table_rows: int = MAX_IMPORTABLE_TABLE_ROWS

    @property
    def ignore_tables(self) -> frozenset[str]:
        """All excluded table names, regardless of provenance -- used by the engine's set logic."""
        return self.tdr_system_tables | self.import_added_aw_tables | self.not_imported_tables
