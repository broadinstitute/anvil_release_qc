"""Tests for Terra-user resolution (anvil_release_qc.auth): Sam-resolved, logged; no network."""

import pytest
import requests

from anvil_release_qc import auth
from anvil_release_qc.auth import resolve_tier_identity, terra_user_email
from anvil_release_qc.config import ResolvedTier

_HUMAN = "sa-user@broadinstitute.org"
_PET = "pet-123@terra-abc.iam.gserviceaccount.com"


def _tier():
    return ResolvedTier(
        name="prod",
        adc_credentials_file=None,
        terra_billing_project="b",
        billing_google_project_id="terra-x",
        azul_token_file=None,
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


# --- resolve_tier_identity ---


def test_resolve_identity_maps_pet_sa_to_terra_user(monkeypatch, caplog):
    # In Terra the ADC is a pet SA; Sam maps it to the human Terra user, and both are logged.
    _patch_guard(monkeypatch, sam_email=_HUMAN, adc_email=_PET)
    with caplog.at_level("INFO", logger="anvil_release_qc"):
        assert resolve_tier_identity(object(), _tier()) == _HUMAN
    assert _HUMAN in caplog.text and _PET in caplog.text


def test_resolve_identity_has_no_allowlist(monkeypatch):
    # Any registered Terra user may run; access is enforced by Terra/TDR/Azul, not by this tool.
    _patch_guard(monkeypatch, sam_email="anyone@broadinstitute.org", adc_email=_PET)
    assert resolve_tier_identity(object(), _tier()) == "anyone@broadinstitute.org"


def test_resolve_identity_fails_when_sam_cannot_resolve(monkeypatch):
    # Credentials that are not a registered Terra user on this tier (e.g. wrong tier) still fail fast.
    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth, "active_identity_email", lambda creds: _PET)
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({}, status=401))
    with pytest.raises(RuntimeError, match="Sam"):
        resolve_tier_identity(object(), _tier())


def test_resolve_identity_audit_lookup_is_best_effort(monkeypatch):
    # The raw-ADC (audit-only) lookup failing must NOT block an otherwise-valid run.
    def boom(creds):
        raise RuntimeError("userinfo unavailable")

    monkeypatch.setattr(auth, "bearer_token", lambda creds: "tok")
    monkeypatch.setattr(auth, "active_identity_email", boom)
    monkeypatch.setattr(auth.requests, "get", lambda *a, **k: _FakeResp({"email": _HUMAN}))
    assert resolve_tier_identity(object(), _tier()) == _HUMAN
