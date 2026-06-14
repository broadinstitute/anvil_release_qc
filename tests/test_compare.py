"""Unit tests for the comparison engine, source helpers, and auth-domain check (no network)."""

from anvil_data_qc.compare.authdomain import check_auth_domain
from anvil_data_qc.compare.engine import compare_inventories
from anvil_data_qc.compare.profile import ComparisonProfile
from anvil_data_qc.compare.sources import TableShape, build_nonempty_query, parse_entity_metadata


def _shape(rows: int, total: int, nonempty: int | None = None) -> TableShape:
    return TableShape(rows, total, total if nonempty is None else nonempty)


def _compare(reference, candidate, profile=None):
    return compare_inventories(
        reference,
        candidate,
        profile or ComparisonProfile(),
        reference_label="TDR",
        candidate_label="AW",
    )


# --- build_nonempty_query ---


def test_build_nonempty_query_scalar_and_array():
    sql = build_nonempty_query(
        "proj", "ds", "anvil_file", [("file_id", "STRING"), ("data_modality", "ARRAY<STRING>")]
    )
    assert "FROM `proj.ds.anvil_file`" in sql
    assert "COUNT(*) AS row_count" in sql
    assert "COUNTIF(`file_id` IS NOT NULL) AS c0" in sql
    assert "COUNTIF(ARRAY_LENGTH(`data_modality`) > 0) AS c1" in sql


# --- parse_entity_metadata ---


def test_parse_entity_metadata():
    meta = {
        "anvil_file": {
            "count": 26016,
            "attributeNames": ["a", "b", "c"],
            "idName": "anvil_file_id",
        },
        "sample": {"count": 3202, "attributeNames": []},
    }
    inv = parse_entity_metadata(meta)
    assert inv["anvil_file"] == TableShape(26016, 3, 3)
    assert inv["sample"].row_count == 3202 and inv["sample"].total_columns == 0


# --- compare_inventories ---


def test_all_pass():
    ref = {"anvil_file": _shape(26016, 11, nonempty=9), "sample": _shape(3202, 17)}
    cand = {"anvil_file": _shape(26016, 13), "sample": _shape(3202, 19)}
    report = _compare(ref, cand)
    assert report.passed
    assert all(t.passed for t in report.tables)


def test_empty_tdr_table_excluded():
    ref = {"anvil_file": _shape(26016, 11, 9), "anvil_diagnosis": _shape(0, 13, 0)}
    cand = {"anvil_file": _shape(26016, 13)}
    report = _compare(ref, cand)
    assert report.passed  # empty TDR table is not required in the AW
    assert "anvil_diagnosis" in report.empty_reference_tables
    assert {t.name for t in report.tables} == {"anvil_file"}


def test_missing_table_fails():
    ref = {"anvil_file": _shape(10, 5, 5), "participant": _shape(7, 3, 3)}
    cand = {"anvil_file": _shape(10, 5)}
    report = _compare(ref, cand)
    assert not report.passed
    p = next(t for t in report.tables if t.name == "participant")
    assert not p.in_candidate and "MISSING" in p.status_label()


def test_aw_only_table_fails():
    ref = {"anvil_file": _shape(10, 5, 5)}
    cand = {"anvil_file": _shape(10, 5), "mystery": _shape(3, 2)}
    report = _compare(ref, cand)
    assert not report.passed
    m = next(t for t in report.tables if t.name == "mystery")
    assert not m.in_reference and "AW-ONLY" in m.status_label()


def test_row_count_mismatch_fails():
    report = _compare({"t": _shape(10, 5, 5)}, {"t": _shape(9, 5)})
    assert not report.passed and not report.tables[0].row_ok


def test_column_shortfall_fails():
    report = _compare({"t": _shape(10, 8, 8)}, {"t": _shape(10, 7)})
    assert not report.passed and not report.tables[0].column_ok


def test_column_added_passes():
    # 12 TDR cols but only 5 non-empty; AW dropped the 7 empties and added refs -> 9 >= 5.
    report = _compare({"t": _shape(10, 12, 5)}, {"t": _shape(10, 9)})
    assert report.passed


def test_system_table_ignored():
    ref = {"t": _shape(10, 3, 3), "datarepo_row_ids": _shape(100317, 2, 2)}
    report = _compare(ref, {"t": _shape(10, 3)})
    assert report.passed
    assert "datarepo_row_ids" in report.ignored_tables
    assert all(t.name != "datarepo_row_ids" for t in report.tables)


# --- auth domain ---


def test_auth_domain_match():
    assert check_auth_domain([], []).passed
    assert check_auth_domain(["AUTH_X", "AUTH_Y"], ["AUTH_Y", "AUTH_X"]).passed  # order-insensitive


def test_auth_domain_mismatch():
    assert not check_auth_domain(["AUTH_X"], []).passed
    assert not check_auth_domain([], ["AUTH_X"]).passed


# --- report render smoke ---


def test_report_render_status():
    assert "PASS" in _compare({"t": _shape(10, 3, 3)}, {"t": _shape(10, 3)}).render()
    assert "FAIL" in _compare({"t": _shape(10, 3, 3)}, {"t": _shape(9, 3)}).render()
