"""Tests for the identity guard (anvil_release_qc.auth): Sam-resolved Terra user; no network."""

import pytest
import requests

from anvil_release_qc import auth
from anvil_release_qc.auth import IdentityMismatchError, assert_tier_identity, terra_user_email
from anvil_release_qc.config import ResolvedTier

_HUMAN = "sa-user@broadinstitute.org"
_PET = "pet-123@terra-abc.iam.gserviceaccount.com"


def _tier(email=_HUMAN, authorized_users=None):
    return ResolvedTier(
        name="prod",
        email=email,
        adc_credentials_file=None,
        terra_billing_project="b",
        billing_google_project_id="terra-x",
        azul_token_file=None,
        authorized_users=authorized_users or [],
        service_urls={"sam": "https://sam.example/"},
    )


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

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


def test_terra_user_email_builds_sam_url(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeResp({"email": _HUMAN})

    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth.requests, "get", fake_get)
    # Correct path whether or not sam_url has a trailing slash (no double slash).
    assert terra_user_email(object(), "https://sam.example/") == _HUMAN
    assert captured["url"] == "https://sam.example/api/users/v2/self"
    assert terra_user_email(object(), "https://sam.example") == _HUMAN
    assert captured["url"] == "https://sam.example/api/users/v2/self"


def test_terra_user_email_http_error_is_wrapped(monkeypatch):
    # A Sam HTTP error (e.g. 404 for an unregistered user) becomes a clear RuntimeError mentioning Sam,
    # not a bare requests.HTTPError.
    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({}, status=404))
    with pytest.raises(RuntimeError, match="Sam"):
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


def test_identity_guard_audit_lookup_is_best_effort(monkeypatch):
    # The raw-ADC (audit-only) lookup failing must NOT block an otherwise-valid run -- Sam is the authority.
    def boom(creds):
        raise RuntimeError("userinfo unavailable")

    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth, "active_identity_email", boom)
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({"email": _HUMAN}))
    assert assert_tier_identity(object(), _tier(email=_HUMAN)) == _HUMAN


def test_identity_guard_allows_additional_authorized_user(monkeypatch):
    # A user listed in authorized_users (not the primary email) is allowed -> multi-operator support.
    _patch_guard(monkeypatch, sam_email="teammate@broadinstitute.org", adc_email=_PET)
    tier = _tier(email=_HUMAN, authorized_users=["teammate@broadinstitute.org"])
    assert assert_tier_identity(object(), tier) == "teammate@broadinstitute.org"


def test_identity_guard_rejects_user_outside_multi_user_allowlist(monkeypatch):
    # With an allowlist (email + authorized_users), a user in NEITHER is still rejected: broadening
    # WHO is allowed must not become "allow anyone".
    _patch_guard(monkeypatch, sam_email="stranger@broadinstitute.org", adc_email=_PET)
    tier = _tier(email=_HUMAN, authorized_users=["teammate@broadinstitute.org"])
    with pytest.raises(IdentityMismatchError):
        assert_tier_identity(object(), tier)
