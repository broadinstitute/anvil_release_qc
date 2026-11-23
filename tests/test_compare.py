"""Unit tests for the comparison engine, source helpers, and auth-domain check (no network)."""

from anvil_release_qc.compare.authdomain import check_auth_domain
from anvil_release_qc.compare.engine import compare_coverage, compare_inventories
from anvil_release_qc.compare.profile import ComparisonProfile
from anvil_release_qc.compare.sources import TableShape, build_nonempty_query, parse_entity_metadata


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
    assert "datarepo_row_ids" in report.tdr_system_tables
    assert all(t.name != "datarepo_row_ids" for t in report.tables)


def test_import_added_aw_table_ignored():
    # duos_dataset_registration is synthesized by the PFB/ADE import (AW-only, no TDR counterpart).
    # It must be excluded -- not flagged AW-ONLY -- and reported under its own provenance category.
    ref = {"t": _shape(10, 3, 3)}
    cand = {"t": _shape(10, 3), "duos_dataset_registration": _shape(1, 5)}
    report = _compare(ref, cand)
    assert report.passed
    assert "duos_dataset_registration" in report.import_added_aw_tables
    assert "duos_dataset_registration" not in report.tdr_system_tables
    assert all(t.name != "duos_dataset_registration" for t in report.tables)


def test_file_inventory_skipped_not_failed():
    # file_inventory is never imported into the AW -- skipped (not MISSING), and noted in the report.
    ref = {"t": _shape(10, 3, 3), "file_inventory": _shape(5000, 8, 8)}
    report = _compare(ref, {"t": _shape(10, 3)})
    assert report.passed
    assert report.not_imported_tables == ["file_inventory"]
    assert all(t.name != "file_inventory" for t in report.tables)
    assert "SKIPPED — file_inventory: not imported" in report.render()


def test_table_over_row_limit_skipped_not_failed():
    # > 350k rows is too large for Terra to import: skipped whether the AW has it or not, never failed.
    ref = {"t": _shape(10, 3, 3), "big": _shape(350_001, 4, 4), "huge": _shape(2_000_000, 4, 4)}
    cand = {"t": _shape(10, 3), "big": _shape(12, 4)}  # a partial AW table is not judged either
    report = _compare(ref, cand)
    assert report.passed
    assert report.too_large_tables == {"big": 350_001, "huge": 2_000_000}
    assert [t.name for t in report.tables] == ["t"]
    text = report.render()
    assert "SKIPPED — big: 350,001 rows in TDR exceeds the 350,000-row Terra import limit" in text
    assert "SKIPPED — huge: 2,000,000 rows" in text


def test_table_at_row_limit_still_compared():
    # Exactly 350k rows is within the limit, so it is compared (and fails if missing).
    report = _compare({"t": _shape(350_000, 3, 3)}, {})
    assert not report.passed
    assert report.too_large_tables == {}


def test_row_limit_is_a_profile_knob():
    profile = ComparisonProfile(max_importable_table_rows=100)
    report = _compare({"t": _shape(101, 3, 3)}, {}, profile)
    assert report.passed
    assert report.too_large_tables == {"t": 101}


# --- compare_coverage (ingestion QC: submitter workspace -> TDR) ---


def _coverage(reference, candidate):
    return compare_coverage(
        reference, candidate, reference_label="submitter workspace", candidate_label="TDR"
    )


def test_ingest_coverage_all_pass():
    # The prod 1000G submitter tables; TDR has them 1:1 plus the harmonized anvil_*/file_inventory/etc.
    submitter = {
        "participant": _shape(3202, 0),
        "pedigree": _shape(3202, 0),
        "qc_result_sample": _shape(3184, 0),
        "sample": _shape(3202, 0),
        "sample_set": _shape(2, 0),
    }
    tdr = dict(submitter)  # same names + row counts on the TDR side
    tdr.update(
        {
            "anvil_activity": _shape(25863, 0),
            "anvil_file": _shape(26016, 0),
            "file_inventory": _shape(26016, 0),
            "workspace_attributes": _shape(24, 0),
            "datarepo_row_ids": _shape(100000, 0),
        }
    )
    report = _coverage(submitter, tdr)
    assert report.passed
    assert len(report.tables) == 5  # driven from the submitter side
    assert {"anvil_activity", "datarepo_row_ids"} <= set(report.out_of_scope_candidate_tables)


def test_ingest_missing_table_fails():
    submitter = {"participant": _shape(10, 0), "pedigree": _shape(7, 0)}
    tdr = {"participant": _shape(10, 0)}  # pedigree never ingested
    report = _coverage(submitter, tdr)
    assert not report.passed
    ped = next(t for t in report.tables if t.name == "pedigree")
    assert not ped.in_candidate and "MISSING from TDR" in ped.status_label()


def test_ingest_row_count_mismatch_fails():
    report = _coverage({"sample": _shape(3202, 0)}, {"sample": _shape(3201, 0)})
    assert not report.passed
    assert not report.tables[0].row_ok
    assert "row-count mismatch" in report.tables[0].status_label()


def test_ingest_render_smoke():
    out = _coverage(
        {"sample": _shape(2, 0)}, {"sample": _shape(2, 0), "anvil_file": _shape(9, 0)}
    ).render()
    assert "Ingestion comparison — PASS" in out
    assert "out of scope" in out


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
