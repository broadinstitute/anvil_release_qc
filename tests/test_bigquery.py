"""Tests for the BigQuery data-volume metrics (anvil_release_qc.bigquery), no network."""

import re

import pytest

from anvil_release_qc import bigquery as bq
from anvil_release_qc.bigquery import (
    DATA_VOLUME_PREFIX,
    BigQueryAccessError,
    DataVolume,
    check_bigquery_access,
    snapshot_data_volume,
)
from anvil_release_qc.compare.engine import compare_inventories
from anvil_release_qc.compare.profile import TDR_SYSTEM_TABLES, ComparisonProfile
from anvil_release_qc.compare.sources import BigQuerySnapshotSource, TableShape


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getitem__(self, key):  # the comparison engine reads some rows by key, not attribute
        return self.__dict__[key]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _FakeClient:
    """Returns column counts for the INFORMATION_SCHEMA query and row counts for the COUNT(*) query."""

    def __init__(self, columns, rows):
        self._columns = columns  # {table: n_cols}
        self._rows = rows  # {table: n_rows}

    def query(self, sql):
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return _Result([_Row(table_name=t, n_cols=c) for t, c in self._columns.items()])
        return _Result([_Row(t=t, n=n) for t, n in self._rows.items()])


def _patch_client(monkeypatch, columns, rows):
    client = _FakeClient(columns, rows)
    monkeypatch.setattr(bq.bigquery, "Client", lambda project, credentials: client)


def test_snapshot_data_volume_excludes_system_and_empty_tables(monkeypatch):
    # t1, t2 are real non-empty tables; empty_t has 0 rows; datarepo_row_ids is a system table.
    _patch_client(
        monkeypatch,
        columns={"t1": 3, "t2": 5, "empty_t": 4, "datarepo_row_ids": 2},
        rows={"t1": 10, "t2": 100, "empty_t": 0, "datarepo_row_ids": 9999},
    )
    vol = snapshot_data_volume(
        object(), "proj", "ds", "billing", ignore_tables={"datarepo_row_ids"}
    )
    # Only t1 + t2 counted: system table excluded, empty table excluded.
    assert vol.total_table_count == 2
    assert vol.total_row_count == 110  # 10 + 100
    assert vol.total_value_count == 530  # 10*3 + 100*5  (schema columns)


def test_snapshot_data_volume_all_empty_or_ignored(monkeypatch):
    _patch_client(
        monkeypatch,
        columns={"datarepo_row_ids": 2, "empty_t": 9},
        rows={"datarepo_row_ids": 50, "empty_t": 0},
    )
    vol = snapshot_data_volume(object(), "p", "d", "b", ignore_tables={"datarepo_row_ids"})
    assert vol == DataVolume(0, 0, 0)


def test_summary_line_has_commas_and_parses_back():
    line = DataVolume(12, 1234567, 89012345).summary_line()
    assert line == (
        f"{DATA_VOLUME_PREFIX} — total_table_count=12 | "
        "total_row_count=1,234,567 | total_value_count=89,012,345"
    )
    # The WDL parse (grep name=[0-9,]+ then strip commas) must recover the raw integers.
    for metric, expected in [
        ("total_table_count", "12"),
        ("total_row_count", "1234567"),
        ("total_value_count", "89012345"),
    ]:
        raw = re.search(rf"{metric}=([0-9,]+)", line).group(1).replace(",", "")
        assert raw == expected


def test_snapshot_data_volume_logs_the_canonical_line(monkeypatch, caplog):
    import logging

    from anvil_release_qc.logging_setup import LOGGER_NAME

    _patch_client(monkeypatch, columns={"t1": 2}, rows={"t1": 5})
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        snapshot_data_volume(object(), "p", "d", "b")
    assert any(DATA_VOLUME_PREFIX in r.message for r in caplog.records)


class _InventoryFakeClient:
    """Serves one ``{table: (n_cols, n_rows)}`` fixture to **every** query shape used by both
    ``snapshot_data_volume`` and the comparison engine's ``BigQuerySnapshotSource``.

    Driving both BigQuery-reading paths from identical data lets us assert they agree on the
    non-system, non-empty table set -- the equality the docs promise (``total_table_count`` ==
    the comparison's "N tables compared"). All columns are reported non-empty, so the fixture's
    row counts alone decide which tables are "real".
    """

    def __init__(self, fixture):
        self._fixture = fixture  # {table: (n_cols, n_rows)}

    def query(self, sql):
        if "n_cols" in sql:  # snapshot_data_volume's column-count query
            return _Result([_Row(table_name=t, n_cols=c) for t, (c, _) in self._fixture.items()])
        if "column_name" in sql:  # BigQuerySnapshotSource._columns()
            return _Result(
                [
                    _Row(table_name=t, column_name=f"col{i}", data_type="STRING")
                    for t, (c, _) in self._fixture.items()
                    for i in range(c)
                ]
            )
        if "row_count" in sql:  # BigQuerySnapshotSource's per-table non-empty query
            table = re.search(r"FROM `[^`]+`", sql).group(0)[6:-1].split(".")[-1]
            ncols, nrows = self._fixture[table]
            return _Result([_Row(row_count=nrows, **{f"c{i}": nrows for i in range(ncols)})])
        # snapshot_data_volume's row-count query (SELECT '<t>' AS t, COUNT(*) AS n ...)
        return _Result([_Row(t=t, n=n) for t, (_, n) in self._fixture.items()])


def test_total_table_count_matches_comparison_tables(monkeypatch):
    """``total_table_count`` must equal the number of TDR tables the comparison engine compares.

    Both quantities derive from BigQuery, via independent code paths (``snapshot_data_volume`` vs
    ``BigQuerySnapshotSource`` + ``compare_inventories``). This drives both from one shared fixture
    and asserts they agree, so a future drift in either path's non-system/non-empty table set --
    which would silently break the equality the docs promise -- is caught here.
    """
    fixture = {
        "anvil_dataset": (8, 1),
        "anvil_biosample": (12, 5000),
        "anvil_donor": (10, 4000),
        "submitter_clinical": (40, 250000),
        "empty_table": (6, 0),  # 0 rows -> dropped by both paths (not imported into the AW)
        "datarepo_row_ids": (1, 9000),  # system table -> excluded by both paths
    }
    client = _InventoryFakeClient(fixture)
    monkeypatch.setattr(bq.bigquery, "Client", lambda project, credentials: client)

    vol = snapshot_data_volume(object(), "proj", "ds", "billing", ignore_tables=TDR_SYSTEM_TABLES)

    # The engine reads the same snapshot via BigQuerySnapshotSource and compares it to a candidate
    # AW that faithfully reflects the non-system, non-empty tables (so the comparison is a clean PASS
    # and its set of compared TDR tables is exactly the baseline).
    reference = BigQuerySnapshotSource(client, "proj", "ds").table_inventory()
    candidate = {
        t: TableShape(row_count=n, total_columns=c, nonempty_columns=c)
        for t, (c, n) in fixture.items()
        if n > 0 and t not in TDR_SYSTEM_TABLES
    }
    report = compare_inventories(
        reference,
        candidate,
        ComparisonProfile(),
        reference_label="TDR",
        candidate_label="AW",
    )

    tdr_tables_compared = sum(1 for t in report.tables if t.in_reference)
    assert vol.total_table_count == tdr_tables_compared == 4
    assert report.passed  # the fixture is a clean match; nothing should fail
    # Row counts come from COUNT(*) on both paths -- they must agree over the same baseline too.
    assert vol.total_row_count == sum(t.reference_rows for t in report.tables if t.in_reference)


# --- check_bigquery_access (the --verify-auth / fail-fast BigQuery preflight) ---


class _OkClient:
    def query(self, sql):
        return _Result([_Row(ok=1)])


class _BoomClient:
    def query(self, sql):
        raise RuntimeError("400 BadRequest ... ProjectId must be non-empty")


def test_check_bigquery_access_ok(monkeypatch):
    # A reachable project: the trivial SELECT 1 succeeds -> no raise, and it logs a success line.
    monkeypatch.setattr(bq.bigquery, "Client", lambda project, credentials: _OkClient())
    check_bigquery_access(object(), "terra-abc123")  # no raise


def test_check_bigquery_access_empty_project_raises():
    # Fail-fast if the project is unset (the field is required, but guard the empty case too).
    with pytest.raises(BigQueryAccessError):
        check_bigquery_access(object(), "")


def test_check_bigquery_access_wraps_backend_error_with_guidance(monkeypatch):
    # The real failure mode: billing_google_project_id set to a non-runnable project (e.g. a Terra
    # billing project). The backend error is wrapped in a guided BigQueryAccessError.
    monkeypatch.setattr(bq.bigquery, "Client", lambda project, credentials: _BoomClient())
    with pytest.raises(BigQueryAccessError) as excinfo:
        check_bigquery_access(object(), "anvil-developer-work")
    msg = str(excinfo.value)
    assert "anvil-developer-work" in msg  # names the offending project
    assert "Google Project ID" in msg  # points at the Terra Workspace Google Project ID fix
