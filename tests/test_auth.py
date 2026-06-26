"""Tests for the identity guard (anvil_data_qc.auth): Sam-resolved Terra user; no network."""

import pytest

from anvil_data_qc import auth
from anvil_data_qc.auth import IdentityMismatchError, assert_tier_identity, terra_user_email
from anvil_data_qc.config import ResolvedTier

_HUMAN = "sa-user@broadinstitute.org"
_PET = "pet-123@terra-abc.iam.gserviceaccount.com"


def _tier(email=_HUMAN):
    return ResolvedTier(
        name="prod",
        email=email,
        adc_credentials_file=None,
        billing_project="b",
        azul_token_file=None,
        service_urls={"sam": "https://sam.example/"},
    )


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("http error")

    def json(self):
        return self._payload


def _patch_guard(monkeypatch, *, sam_email, adc_email=_PET):
    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")  # no real cred refresh
    monkeypatch.setattr(auth, "active_identity_email", lambda creds: adc_email)  # raw ADC (pet SA)
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({"email": sam_email}))


# --- terra_user_email (Sam /api/users/v2/self) ---


def test_terra_user_email_resolves_via_sam(monkeypatch):
    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({"email": _HUMAN}))
    assert terra_user_email(object(), "https://sam.example/") == _HUMAN


def test_terra_user_email_missing_email_raises(monkeypatch):
    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({}))
    with pytest.raises(RuntimeError):
        terra_user_email(object(), "https://sam.example/")


# --- assert_tier_identity ---


def test_identity_guard_ok_when_pet_sa_maps_to_tier_user(monkeypatch):
    # In Terra the ADC is a pet SA, but Sam maps it to the human tier account -> guard passes.
    _patch_guard(monkeypatch, sam_email=_HUMAN, adc_email=_PET)
    assert assert_tier_identity(object(), _tier(email=_HUMAN)) == _HUMAN


def test_identity_guard_case_insensitive(monkeypatch):
    _patch_guard(monkeypatch, sam_email=_HUMAN.upper(), adc_email=_PET)
    assert assert_tier_identity(object(), _tier(email=_HUMAN)) == _HUMAN.upper()


def test_identity_guard_mismatch_raises(monkeypatch):
    # Sam resolves a different Terra user than configured -> wrong-tier guard fires.
    _patch_guard(monkeypatch, sam_email="someone-else@broadinstitute.org", adc_email=_PET)
    with pytest.raises(IdentityMismatchError):
        assert_tier_identity(object(), _tier(email=_HUMAN))
