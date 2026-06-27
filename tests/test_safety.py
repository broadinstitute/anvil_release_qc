"""Tests for the controlled-access safety gate (anvil_data_qc.safety) — fail-closed behavior."""

import pytest

from anvil_data_qc.clients.tdr import snapshot_consent_code
from anvil_data_qc.safety import (
    ControlledAccessNotApprovedError,
    SignedUrl,
    SignedUrlProvenanceError,
    assert_consent_permitted,
    assert_open_access,
    is_open_access,
    tdr_export_allowed_prefixes,
    verify_pfb_handoff,
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
    # Default (opt-in off): open-access (NRES) proceeds without raising.
    assert_consent_permitted("snap-1", "NRES", allow_controlled_access=False)


@pytest.mark.parametrize("code", ["", None, "GRU", "nres", "phs000123"])
def test_consent_gate_blocks_controlled_when_opt_in_off(code):
    # Fail closed by default: anything but NRES is blocked when controlled-access is not opted in.
    with pytest.raises(ControlledAccessNotApprovedError):
        assert_consent_permitted("snap-1", code, allow_controlled_access=False)


@pytest.mark.parametrize("code", ["NRES", "GRU", "HMB", "phs000123", "", None])
def test_consent_gate_allows_any_code_when_opt_in_on(code):
    # Opt-in on (security-approved): any consentCode -- including empty/unknown -- is permitted (no raise).
    assert_consent_permitted("snap-1", code, allow_controlled_access=True)


# tdr-qc uses the same consent gate as ade-qc (assert_consent_permitted, covered above): open-access by
# default, any consentCode when allow_controlled_access is set. (tdrSyncPermissions=false was confirmed
# correct + safe for controlled data on 2026-06-27 -- the auth domain is applied independently and the
# QC auth-domain check is the backstop; see docs/tdr_export_flow.md.)


# --- TDR export provenance (layer 3): per-snapshot export bucket on GCS ---


def test_tdr_export_allowed_prefixes_is_per_snapshot_bucket():
    assert tdr_export_allowed_prefixes("datarepo-13858a9f") == (
        "https://storage.googleapis.com/datarepo-13858a9f-snapshot-export-bucket/",
    )


def _tdr_handoff(url):
    # Destination is fine (same tier); exercise the provenance check against the per-snapshot bucket.
    verify_pfb_handoff(
        SignedUrl(url),
        tier_name="prod",
        expected_firecloud_host="api.firecloud.org",
        destination_host="api.firecloud.org",
        allowed_prefixes=tdr_export_allowed_prefixes("datarepo-13858a9f"),
    )


def test_tdr_handoff_accepts_matching_bucket():
    _tdr_handoff(
        "https://storage.googleapis.com/datarepo-13858a9f-snapshot-export-bucket/job1/manifest.json"
        "?X-Goog-Signature=z"
    )  # no raise


def test_tdr_handoff_rejects_wrong_bucket():
    with pytest.raises(SignedUrlProvenanceError):
        _tdr_handoff(
            "https://storage.googleapis.com/datarepo-OTHER-snapshot-export-bucket/job1/manifest.json?sig=z"
        )


def test_tdr_handoff_rejects_sibling_bucket_prefix():
    # The trailing "/" anchors the prefix at the bucket boundary: a longer sibling name must not match.
    with pytest.raises(SignedUrlProvenanceError):
        _tdr_handoff(
            "https://storage.googleapis.com/datarepo-13858a9f-snapshot-export-bucket-evil/job1/manifest.json?sig=z"
        )


def test_tdr_handoff_rejects_non_gcs_host():
    with pytest.raises(SignedUrlProvenanceError):
        _tdr_handoff(
            "https://evil.example/datarepo-13858a9f-snapshot-export-bucket/job1/manifest.json?sig=z"
        )
