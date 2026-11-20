"""Tabular data sources for QC comparison.

A `TabularDataSource` reports a uniform per-table inventory -- row count and column counts -- so the
engine can compare the TDR snapshot (BigQuery) against the Analysis Workspace (Rawls) without knowing
where the data lives. The inventory carries what T1-T2 need; richer population (ids, cell values for
T3-T4) extends this seam additively.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from google.cloud import bigquery
from ops_utils.terra_util import TerraWorkspace


@dataclass(frozen=True)
class TableShape:
    """Per-table shape: row count, total columns, and non-empty columns.

    On the AW (Rawls) side every attribute present is non-empty (Terra drops empty source columns on
    import), so ``nonempty_columns == total_columns`` there. On the TDR snapshot (BigQuery) side,
    ``nonempty_columns`` excludes all-null / all-empty-array columns -- the baseline the import keeps.
    """

    row_count: int
    total_columns: int
    nonempty_columns: int


class TabularDataSource(ABC):
    """A source of tabular data that can report its per-table inventory."""

    label: str

    @abstractmethod
    def table_inventory(self) -> dict[str, TableShape]:
        """Return ``{table_name: TableShape}`` for every table this source exposes."""
        raise NotImplementedError


# --- TDR snapshot (BigQuery) ---


def build_nonempty_query(
    project: str, dataset: str, table: str, columns: list[tuple[str, str]]
) -> str:
    """SQL returning the row count and, per column, the count of non-empty values.

    Columns are aliased positionally (``c0``, ``c1`` ...) so the query is collision-free regardless of
    column name. A value counts as non-empty when an array is non-empty (``ARRAY_LENGTH > 0``) or a
    scalar ``IS NOT NULL``.
    """
    parts = ["COUNT(*) AS row_count"]
    for i, (name, dtype) in enumerate(columns):
        col = f"`{name}`"
        cond = (
            f"ARRAY_LENGTH({col}) > 0"
            if dtype.upper().startswith("ARRAY")
            else f"{col} IS NOT NULL"
        )
        parts.append(f"COUNTIF({cond}) AS c{i}")
    return f"SELECT {', '.join(parts)} FROM `{project}.{dataset}.{table}`"


class BigQuerySnapshotSource(TabularDataSource):
    """Reads the TDR snapshot's tables from BigQuery (the reference / source of truth)."""

    def __init__(self, client: bigquery.Client, project: str, dataset: str) -> None:
        self.client = client
        self.project = project
        self.dataset = dataset
        self.label = f"TDR snapshot BigQuery {project}.{dataset}"

    def _columns(self) -> dict[str, list[tuple[str, str]]]:
        query = (
            f"SELECT table_name, column_name, data_type "
            f"FROM `{self.project}.{self.dataset}`.INFORMATION_SCHEMA.COLUMNS "
            f"ORDER BY table_name, ordinal_position"
        )
        out: dict[str, list[tuple[str, str]]] = {}
        for row in self.client.query(query).result():
            out.setdefault(row.table_name, []).append((row.column_name, row.data_type))
        return out

    def table_inventory(self) -> dict[str, TableShape]:
        inventory: dict[str, TableShape] = {}
        for table, columns in self._columns().items():
            sql = build_nonempty_query(self.project, self.dataset, table, columns)
            row = next(iter(self.client.query(sql).result()))
            nonempty = sum(1 for i in range(len(columns)) if (row[f"c{i}"] or 0) > 0)
            inventory[table] = TableShape(
                row_count=row["row_count"], total_columns=len(columns), nonempty_columns=nonempty
            )
        return inventory


# --- Analysis Workspace (Rawls) ---


def parse_entity_metadata(metadata: dict) -> dict[str, TableShape]:
    """Convert Rawls ``GET .../entities`` metadata into ``{entity_type: TableShape}``."""
    inventory: dict[str, TableShape] = {}
    for entity_type, meta in metadata.items():
        attrs = len(meta.get("attributeNames", []))
        inventory[entity_type] = TableShape(
            row_count=meta.get("count", 0), total_columns=attrs, nonempty_columns=attrs
        )
    return inventory


class RawlsWorkspaceSource(TabularDataSource):
    """Reads a Terra workspace's data tables from Rawls.

    Used both as the *candidate* (an Analysis Workspace, for release QC) and as the *reference* (a
    submitter/ADSW workspace, for ingestion QC) -- pass ``label`` to describe its role in the report.
    """

    def __init__(self, workspace: TerraWorkspace, *, label: str | None = None) -> None:
        self.workspace = workspace
        self.label = label or f"AW {workspace.billing_project}/{workspace.workspace_name}"

    def table_inventory(self) -> dict[str, TableShape]:
        # use_cache=False: right after an import the cached entity counts can be stale, and QC compares
        # exact row counts.
        return parse_entity_metadata(self.workspace.get_workspace_entity_info(use_cache=False).json())
