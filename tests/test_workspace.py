"""Tests for workspace naming (pure logic, no network)."""

from datetime import datetime

from anvil_release_qc.workspace import (
    ADE_NAME_INFIX,
    TDR_NAME_INFIX,
    sanitize,
    workspace_name,
)


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
        "sa-mbaumann_qc_anvil_ade_"
        "ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS_20240113_ANV5_202409261935_202606101431"
    )


def test_workspace_name_minute_granular_avoids_collisions():
    snap = "DS One"
    a = workspace_name("u@x.org", snap, when=datetime(2026, 6, 10, 14, 31))
    b = workspace_name("u@x.org", snap, when=datetime(2026, 6, 10, 14, 32))
    assert a != b  # a one-minute-later rerun gets a distinct name


def test_workspace_name_infix_default_is_ade():
    # The infix defaults to the ADE/Azul flow (backward-compatible with the pre-rename behavior).
    when = datetime(2026, 6, 10, 14, 31)
    assert ADE_NAME_INFIX == "qc_anvil_ade"
    name = workspace_name("u@x.org", "DS One", when=when)
    assert f"_{ADE_NAME_INFIX}_" in name


def test_workspace_name_infix_selects_flow():
    # Each export flow selects its own infix so retained workspaces name their provenance.
    when = datetime(2026, 6, 10, 14, 31)
    ade = workspace_name("u@x.org", "DS One", when=when, infix=ADE_NAME_INFIX)
    tdr = workspace_name("u@x.org", "DS One", when=when, infix=TDR_NAME_INFIX)
    assert ade == "u_qc_anvil_ade_DS_One_202606101431"
    assert tdr == "u_qc_anvil_tdr_DS_One_202606101431"
    assert ade != tdr
