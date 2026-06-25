"""Tests for the controlled-access safety gate (anvil_data_qc.safety) — fail-closed behavior."""

import pytest

from anvil_data_qc.clients.tdr import snapshot_consent_code
from anvil_data_qc.safety import (
    ControlledAccessNotApprovedError,
    assert_consent_permitted,
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


# --- assert_consent_permitted: the controlled-access opt-in gate ---


def test_consent_gate_open_access_default():
    # Default (opt-in off): open-access proceeds and returns False (the normal, non-controlled path).
    assert assert_consent_permitted("snap-1", "NRES", allow_controlled_access=False) is False


@pytest.mark.parametrize("code", ["", None, "GRU", "nres", "phs000123"])
def test_consent_gate_blocks_controlled_when_opt_in_off(code):
    # Fail closed by default: anything but NRES is blocked when controlled-access is not opted in.
    with pytest.raises(ControlledAccessNotApprovedError):
        assert_consent_permitted("snap-1", code, allow_controlled_access=False)


@pytest.mark.parametrize("code", ["NRES", "GRU", "HMB", "phs000123", "", None])
def test_consent_gate_allows_any_code_when_opt_in_on(code):
    # Opt-in on (security-approved): any consentCode -- including empty/unknown -- is permitted, and
    # returns True so the caller emits the controlled-access audit log.
    assert assert_consent_permitted("snap-1", code, allow_controlled_access=True) is True
