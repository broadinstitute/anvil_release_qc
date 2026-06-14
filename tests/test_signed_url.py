"""Tests for signed-URL containment, hand-off verification, and security guards (anvil_data_qc.safety)."""

import json
import logging
from pathlib import Path

import pytest

from anvil_data_qc.logging_setup import LOGGER_NAME
from anvil_data_qc.safety import (
    SIGNED_URL_ALLOWED_PREFIXES,
    SignedUrl,
    SignedUrlDestinationError,
    SignedUrlProvenanceError,
    verify_pfb_handoff,
)

# A signed-URL-shaped value (fake credentials, not real, not AWS-key-shaped -> git-secrets-safe).
_RAW = (
    "https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-dev-storage-anvildev.us-east-1/"
    "manifests/x.avro?AWSAccessKeyId=FAKEKEYID&Signature=fakesigvalue&x-amz-security-token=faketoken"
)
_SECRETS = ("FAKEKEYID", "fakesigvalue", "faketoken")
_DEV_PREFIX = ("https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-dev-storage-anvildev",)
_DEV_FIRECLOUD = "firecloud-orchestration.dsde-dev.broadinstitute.org"
_SRC = Path(__file__).resolve().parent.parent / "src" / "anvil_data_qc"


# --- SignedUrl containment ---


def test_reveal_returns_raw():
    assert SignedUrl(_RAW).reveal() == _RAW


def test_location_and_host_drop_the_query():
    su = SignedUrl(_RAW)
    assert su.host == "s3.amazonaws.com"
    assert su.location.endswith("/manifests/x.avro")
    assert "?" not in su.location
    for secret in _SECRETS:
        assert secret not in su.location


def test_repr_str_format_redact_the_secret():
    su = SignedUrl(_RAW)
    for rendered in (repr(su), str(su), f"{su}", "{}".format(su)):
        for secret in _SECRETS:
            assert secret not in rendered


def test_not_json_serializable():
    with pytest.raises(TypeError):
        json.dumps({"url": SignedUrl(_RAW)})


# --- verify_pfb_handoff ---


def test_handoff_ok_for_matching_destination_and_provenance():
    verify_pfb_handoff(
        SignedUrl(_RAW),
        tier_name="dev",
        expected_firecloud_host=_DEV_FIRECLOUD,
        destination_host=_DEV_FIRECLOUD,
        allowed_prefixes=_DEV_PREFIX,
    )  # does not raise


def test_handoff_blocks_wrong_destination():
    with pytest.raises(SignedUrlDestinationError):
        verify_pfb_handoff(
            SignedUrl(_RAW),
            tier_name="dev",
            expected_firecloud_host=_DEV_FIRECLOUD,
            destination_host="evil.example.org",
            allowed_prefixes=_DEV_PREFIX,
        )


def test_handoff_blocks_wrong_provenance():
    other = SignedUrl("https://s3.amazonaws.com/some-other-bucket/x.avro?Signature=fakesig")
    with pytest.raises(SignedUrlProvenanceError):
        verify_pfb_handoff(
            other,
            tier_name="dev",
            expected_firecloud_host=_DEV_FIRECLOUD,
            destination_host=_DEV_FIRECLOUD,
            allowed_prefixes=_DEV_PREFIX,
        )


def test_handoff_empty_allowlist_warns_but_proceeds(caplog):
    # A tier with no configured allow-list: provenance is warned, not blocked (destination still
    # enforced). Both dev and prod ARE configured (see test below); this covers the generic branch.
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verify_pfb_handoff(
            SignedUrl(_RAW),
            tier_name="futuretier",
            expected_firecloud_host="import.example.org",
            destination_host="import.example.org",
            allowed_prefixes=(),
        )
    assert any("allow-list" in r.message.lower() for r in caplog.records)


def test_prod_provenance_is_configured_and_enforced():
    prod_prefixes = SIGNED_URL_ALLOWED_PREFIXES["prod"]
    assert prod_prefixes, "prod provenance allow-list must be configured (enforced, not warned)"
    good = SignedUrl(
        "https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-prod-storage-anvilprod.us-east-1/"
        "manifests/x.avro?Signature=fakesig"
    )
    verify_pfb_handoff(
        good,
        tier_name="prod",
        expected_firecloud_host="api.firecloud.org",
        destination_host="api.firecloud.org",
        allowed_prefixes=prod_prefixes,
    )  # real prod-bucket URL passes
    # A dev-bucket URL in a prod run must be blocked -- the cross-tier catch.
    dev_url_in_prod = SignedUrl(
        "https://s3.amazonaws.com/edu-ucsc-gi-platform-anvil-dev-storage-anvildev.us-east-1/"
        "manifests/x.avro?Signature=fakesig"
    )
    with pytest.raises(SignedUrlProvenanceError):
        verify_pfb_handoff(
            dev_url_in_prod,
            tier_name="prod",
            expected_firecloud_host="api.firecloud.org",
            destination_host="api.firecloud.org",
            allowed_prefixes=prod_prefixes,
        )


# --- security guards (meta-tests over the source tree) ---


def test_no_tls_verification_disabled_in_src():
    for py in _SRC.rglob("*.py"):
        assert "verify=false" not in py.read_text().lower().replace(" ", ""), (
            f"{py} appears to disable TLS verification"
        )


def test_signed_url_revealed_only_in_firecloud():
    # The raw signed URL must be unwrapped at exactly one call site. (safety.py defines reveal()).
    callers = sorted(
        p.name for p in _SRC.rglob("*.py") if p.name != "safety.py" and ".reveal()" in p.read_text()
    )
    assert callers == ["firecloud.py"], (
        f"signed-URL .reveal() must be called only in firecloud.py: {callers}"
    )
