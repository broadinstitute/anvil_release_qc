"""Tests for workspace naming (pure logic, no network)."""

from datetime import datetime

from anvil_data_qc.workspace import sanitize, workspace_name


def test_sanitize_collapses_disallowed_runs():
    assert sanitize("My Cool Dataset (v2)!!") == "My_Cool_Dataset_v2"
    assert sanitize("a@@@b//c") == "a_b_c"
    assert sanitize("__edges__") == "edges"
    assert sanitize("ANVIL_CCDG_x_20240113") == "ANVIL_CCDG_x_20240113"  # already clean


def test_workspace_name_format():
    when = datetime(2026, 6, 10, 14, 31)
    name = workspace_name(
        "sa-mbaumann@broadinstitute.org",
        "ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS_20240113_ANV5_202409261935",
        when=when,
    )
    assert name == (
        "sa-mbaumann_anvil_azul_"
        "ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS_20240113_ANV5_202409261935_202606101431"
    )


def test_workspace_name_minute_granular_avoids_collisions():
    snap = "DS One"
    a = workspace_name("u@x.org", snap, when=datetime(2026, 6, 10, 14, 31))
    b = workspace_name("u@x.org", snap, when=datetime(2026, 6, 10, 14, 32))
    assert a != b  # a one-minute-later rerun gets a distinct name
