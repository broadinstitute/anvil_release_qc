"""BigQuery access to the TDR snapshot (the system of record).

Two uses: resolve the Azul ``dataset_id`` from the snapshot's ``anvil_dataset`` table (the ADE flow),
and compute the snapshot's **data-volume metrics** (``snapshot_data_volume``) -- table/row/value counts
that characterize how much tabular data a workspace import must absorb (see docs/tdr_export_flow.md).
See docs/export_import_flow.md section 1.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass

from google.auth.credentials import Credentials
from google.cloud import bigquery

from anvil_release_qc.logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

#: Marker prefix for the canonical data-volume summary line (the WDLs grep for this).
DATA_VOLUME_PREFIX = "Data volume (TDR snapshot)"


@dataclass(frozen=True)
class DataVolume:
    """Snapshot data-volume metrics over the **non-system, non-empty** tables.

    See ``snapshot_data_volume`` for the precise definition. ``total_value_count`` uses each table's
    schema column count (a deliberate, documented over-approximation -- empty columns are dropped on
    import but counting them avoids an expensive per-column scan).
    """

    total_table_count: int
    total_row_count: int
    total_value_count: int

    def summary_line(self) -> str:
        """One canonical line for the detailed log / QC report.

        Values carry **comma thousands separators** for at-a-glance readability, and fields are
        ``|``-separated so those value commas can't be confused with field separators. The WDLs strip
        the commas before ``read_int`` to produce the integer workflow outputs. Keep the
        ``<name>=<number>`` shape parseable -- it is a stable contract.
        """
        return (
            f"{DATA_VOLUME_PREFIX} — "
            f"total_table_count={self.total_table_count:,} | "
            f"total_row_count={self.total_row_count:,} | "
            f"total_value_count={self.total_value_count:,}"
        )


class BigQueryAccessError(RuntimeError):
    """Raised when BigQuery jobs cannot run in the configured ``billing_google_project_id``.

    Carries a guided message for the common setup mistake: ``billing_google_project_id`` must be a real
    Google Cloud Project ID (the Terra Workspace Google Project ID, e.g. ``terra-xxxxxxxx``) where the
    active identity can run BigQuery jobs -- NOT the Terra billing project. The CLI presents this cleanly.
    """


def check_bigquery_access(credentials: Credentials, billing_google_project_id: str) -> None:
    """Preflight: verify a trivial BigQuery job can run in ``billing_google_project_id``.

    Runs ``SELECT 1`` billed to ``billing_google_project_id`` -- read-only and scans no data (it does
    create a lightweight BigQuery job record) -- so a missing/misconfigured project (e.g. the Terra
    billing project mistakenly used as a GCP project id) surfaces here, including in ``--verify-auth``,
    with a clear message instead of as a mid-run traceback. Raises ``BigQueryAccessError`` on any failure
    (a wrong project id, missing BigQuery Job User, or a transient BigQuery/credentials error).
    """
    if not billing_google_project_id:
        raise BigQueryAccessError(
            "billing_google_project_id is not set. It must be a real Google Cloud Project ID (the Terra "
            "Workspace Google Project ID, e.g. terra-xxxxxxxx) where BigQuery jobs can run."
        )
    client = bigquery.Client(project=billing_google_project_id, credentials=credentials)
    try:
        next(iter(client.query("SELECT 1 AS ok").result()), None)
    except Exception as exc:
        raise BigQueryAccessError(
            f"Cannot run BigQuery jobs in billing_google_project_id={billing_google_project_id!r}: {exc}. "
            "Most often this means billing_google_project_id is wrong: it must be a real Google Cloud "
            "Project ID -- the Terra Workspace Google Project ID (Terra workspace dashboard -> Cloud "
            "Information -> Google Project ID, e.g. terra-xxxxxxxx), NOT the Terra billing project -- and "
            "the active identity needs BigQuery Job User on it (see config.example.yaml). It may also be a "
            "transient BigQuery error or expired credentials, in which case retry / re-authenticate."
        ) from exc
    logger.info("BigQuery access OK: can run jobs in %s.", billing_google_project_id)


def resolve_dataset_id(
    credentials: Credentials, bq_project: str, bq_dataset: str, billing_google_project_id: str
) -> str:
    """Return the single ``dataset_id`` from the snapshot's ``anvil_dataset`` table.

    ``bq_project``/``bq_dataset`` identify the snapshot's BigQuery dataset (from TDR); the query runs in
    (is billed to) ``billing_google_project_id``. Zero or more than one row is treated as an error / QC
    finding.
    """
    client = bigquery.Client(project=billing_google_project_id, credentials=credentials)
    # Table identifiers cannot be parameterized; bq_project/bq_dataset come from TDR (trusted).
    query = f"SELECT dataset_id FROM `{bq_project}.{bq_dataset}.anvil_dataset`"
    logger.info("Resolving dataset_id: %s", query)
    rows = list(client.query(query).result())
    if len(rows) != 1:
        raise ValueError(
            f"Expected exactly one row in `{bq_project}.{bq_dataset}.anvil_dataset`, got {len(rows)}."
        )
    dataset_id = rows[0]["dataset_id"]
    logger.info("Resolved Azul dataset_id = %s", dataset_id)
    return dataset_id


def snapshot_data_volume(
    credentials: Credentials,
    bq_project: str,
    bq_dataset: str,
    billing_google_project_id: str,
    *,
    ignore_tables: Collection[str] = (),
) -> DataVolume:
    """Compute the snapshot's data-volume metrics from BigQuery structural metadata.

    Counts span the **non-system, non-empty** tables -- the QC comparison's TDR baseline *before* it
    skips tables the import does not carry (``file_inventory``, tables over the 350k-row import limit),
    so ``total_table_count`` can exceed the comparison's "N tables compared" by those skipped tables:

    - **System tables excluded** via ``ignore_tables`` (e.g. ``datarepo_row_ids`` -- TDR plumbing,
      never imported, and it would roughly double the counts).
    - **Empty tables excluded** (``row_count == 0``) -- Terra does not import them, so from the Data
      Tables' perspective they are not "real" tables.

    ``total_value_count`` = Σ(rows × **schema** columns). Using schema (not non-empty) columns is a
    deliberate, documented over-approximation: empty columns are also dropped on import, but excluding
    them would require an expensive per-column ``COUNTIF`` scan -- exactly what we avoid on these large
    datasets. The two queries here (one ``INFORMATION_SCHEMA.COLUMNS``, one ``COUNT(*)`` per table) do
    **not** scan column values, so this stays cheap even on huge datasets and is computed up front --
    available even if a later export/import times out or errors. Billed to ``billing_google_project_id``.
    """
    client = bigquery.Client(project=billing_google_project_id, credentials=credentials)
    ignore = set(ignore_tables)
    # 1. Tables + schema column counts (one cheap INFORMATION_SCHEMA query).
    col_query = (
        f"SELECT table_name, COUNT(*) AS n_cols "
        f"FROM `{bq_project}.{bq_dataset}`.INFORMATION_SCHEMA.COLUMNS "
        f"GROUP BY table_name"
    )
    columns = {
        row.table_name: row.n_cols
        for row in client.query(col_query).result()
        if row.table_name not in ignore
    }
    if not columns:
        return DataVolume(0, 0, 0)
    # 2. Row counts per table in one query. COUNT(*) does not scan column values (cheap even on huge
    #    tables/views). Table identifiers cannot be parameterized; they come from TDR (trusted) and are
    #    backtick-quoted, like resolve_dataset_id.
    row_query = " UNION ALL ".join(
        f"SELECT '{table}' AS t, COUNT(*) AS n FROM `{bq_project}.{bq_dataset}.{table}`"
        for table in columns
    )
    rows = {row.t: row.n for row in client.query(row_query).result()}
    # Non-empty tables only (empty tables are not imported into the workspace).
    nonempty = {table: columns[table] for table in columns if rows.get(table, 0) > 0}
    volume = DataVolume(
        total_table_count=len(nonempty),
        total_row_count=sum(rows[table] for table in nonempty),
        total_value_count=sum(rows[table] * nonempty[table] for table in nonempty),
    )
    logger.info("%s", volume.summary_line())
    return volume
