"""BigQuery access to the TDR snapshot (the system of record).

Step 1 of the flow: resolve the Azul ``dataset_id`` from the snapshot's ``anvil_dataset`` table.
Every AnVIL snapshot has an ``anvil_dataset`` table with a ``dataset_id`` column and exactly one row.
See docs/export_import_flow.md section 1.
"""

from __future__ import annotations

import logging

from google.auth.credentials import Credentials
from google.cloud import bigquery

from .logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


def resolve_dataset_id(
    credentials: Credentials, bq_project: str, bq_dataset: str, billing_project: str
) -> str:
    """Return the single ``dataset_id`` from the snapshot's ``anvil_dataset`` table.

    ``bq_project``/``bq_dataset`` identify the snapshot's BigQuery dataset (from TDR); the query is
    billed to ``billing_project``. Zero or more than one row is treated as an error / QC finding.
    """
    client = bigquery.Client(project=billing_project, credentials=credentials)
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
