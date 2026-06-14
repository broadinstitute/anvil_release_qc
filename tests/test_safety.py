"""Tests for the controlled-access safety gate (anvil_data_qc.safety) — fail-closed behavior."""

import pytest

from anvil_data_qc.clients.tdr import snapshot_consent_code
from anvil_data_qc.safety import (
    ControlledAccessNotApprovedError,
    assert_open_access,
    is_open_access,
)


def test_is_open_access_only_exact_nres():
    assert is_open_access("NRES") is True
    # Fail closed: empty, missing, wrong case, padded, or any other code is NOT open-access.
    for value in ["", None, "nres", "NRES ", " NRES", "GRU", "HMB", "phs000123"]:
        assert is_open_access(value) is False


def test_assert_open_access_proceeds_on_nres():
    assert_open_access("snap-1", "NRES")  # does not raise


@pytest.mark.parametrize("code", ["", None, "GRU", "nres", "phs000123"])
def test_assert_open_access_blocks_non_nres(code):
    with pytest.raises(ControlledAccessNotApprovedError) as excinfo:
        assert_open_access("snap-1", code)
    assert "controlled-access" in str(excinfo.value).lower()


def test_snapshot_consent_code_extraction():
    assert snapshot_consent_code({"consentCode": "NRES"}) == "NRES"
    assert snapshot_consent_code({"consentCode": ""}) == ""
    assert snapshot_consent_code({}) is None
