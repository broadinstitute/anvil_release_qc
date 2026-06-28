"""Tests for the BigQuery data-volume metrics (anvil_data_qc.bigquery), no network."""

import re

from anvil_data_qc import bigquery as bq
from anvil_data_qc.bigquery import DATA_VOLUME_PREFIX, DataVolume, snapshot_data_volume


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


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

    from anvil_data_qc.logging_setup import LOGGER_NAME

    _patch_client(monkeypatch, columns={"t1": 2}, rows={"t1": 5})
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        snapshot_data_volume(object(), "p", "d", "b")
    assert any(DATA_VOLUME_PREFIX in r.message for r in caplog.records)
