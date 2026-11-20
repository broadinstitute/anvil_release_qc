"""Tests for Azul access-token handling (anvil_release_qc.azul_auth), no network."""

import pytest

from anvil_release_qc import azul_auth
from anvil_release_qc.azul_auth import (
    AzulReindexingError,
    AzulTokenError,
    assert_azul_not_reindexing,
    azul_indexing_backlog,
    discover_azul_client_id,
    expected_client_id,
    extract_bearer_token,
    read_azul_token,
    token_from_file_text,
    validate_azul_token,
    write_azul_token,
)
from anvil_release_qc.config import ResolvedTier

_CLIENT_ID = "111-abcDEF.apps.googleusercontent.com"
_EMAIL = "dev-user@test.firecloud.org"


def _tier(token_file=None, client_id=None):
    return ResolvedTier(
        name="dev",
        email=_EMAIL,
        adc_credentials_file=None,
        terra_billing_project="b",
        billing_google_project_id="terra-x",
        azul_token_file=token_file,
        azul_oauth2_client_id=client_id,
        service_urls={"azul": "https://azul.example/"},
    )


class _FakeResp:
    def __init__(self, text="", status=200, json_data=None):
        self.text = text
        self.status_code = status
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("http error")

    def json(self):
        return self._json


def _info(azp=_CLIENT_ID, email=_EMAIL, expires_in="3600"):
    return {"azp": azp, "email": email, "expires_in": expires_in}


# --- extract_bearer_token ---


def test_extract_bearer_from_curl():
    curl = "curl -X 'GET' 'https://azul.example/index' -H 'Authorization: Bearer abc.def-123_456'"
    assert extract_bearer_token(curl) == "abc.def-123_456"


def test_extract_bearer_raw_token():
    assert extract_bearer_token("  abc.def-123_456  ") == "abc.def-123_456"


def test_extract_bearer_rejects_garbage():
    with pytest.raises(AzulTokenError):
        extract_bearer_token("this is not a token")


# --- discover_azul_client_id ---


def test_discover_client_id(monkeypatch):
    js = f'const client_id = "{_CLIENT_ID}";\n  clientId: client_id,'
    monkeypatch.setattr(azul_auth.requests, "get", lambda *a, **k: _FakeResp(js))
    assert discover_azul_client_id("https://azul.example/") == _CLIENT_ID


def test_discover_client_id_not_found(monkeypatch):
    monkeypatch.setattr(azul_auth.requests, "get", lambda *a, **k: _FakeResp("no id here"))
    with pytest.raises(AzulTokenError):
        discover_azul_client_id("https://azul.example/")


# --- reindexing preflight (/health/progress) ---


def _progress(up=True, bundles=0, documents=0):
    return {
        "up": up,
        "progress": {"up": up, "unindexed_bundles": bundles, "unindexed_documents": documents},
    }


def test_indexing_backlog_parses(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests,
        "get",
        lambda *a, **k: _FakeResp(json_data=_progress(bundles=5, documents=3)),
    )
    assert azul_indexing_backlog("https://azul.example/") == (True, 5, 3)


def test_indexing_backlog_non_object_body_raises_valueerror(monkeypatch):
    # A valid-JSON but non-object body must surface as ValueError (which callers fail-open on), NOT
    # AttributeError -- else a malformed 200 escapes fail-open and becomes a false QC FAIL.
    monkeypatch.setattr(
        azul_auth.requests, "get", lambda *a, **k: _FakeResp(json_data=["not", "an", "object"])
    )
    with pytest.raises(ValueError):
        azul_indexing_backlog("https://azul.example/")


def test_indexing_backlog_nondict_progress_raises_valueerror(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests,
        "get",
        lambda *a, **k: _FakeResp(json_data={"up": True, "progress": "reindexing"}),
    )
    with pytest.raises(ValueError):
        azul_indexing_backlog("https://azul.example/")


def test_indexing_backlog_non_integer_count_raises_valueerror(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests,
        "get",
        lambda *a, **k: _FakeResp(json_data={"up": True, "progress": {"unindexed_bundles": [1]}}),
    )
    with pytest.raises(ValueError):
        azul_indexing_backlog("https://azul.example/")


def test_not_reindexing_when_idle(monkeypatch):
    monkeypatch.setattr(azul_auth.requests, "get", lambda *a, **k: _FakeResp(json_data=_progress()))
    assert assert_azul_not_reindexing("https://azul.example/") is None  # does not raise


def test_reindexing_blocks_on_bundles(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests, "get", lambda *a, **k: _FakeResp(json_data=_progress(bundles=507118))
    )
    with pytest.raises(AzulReindexingError, match="indexing"):
        assert_azul_not_reindexing("https://azul.example/")


def test_reindexing_blocks_on_documents(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests, "get", lambda *a, **k: _FakeResp(json_data=_progress(documents=42))
    )
    with pytest.raises(AzulReindexingError):
        assert_azul_not_reindexing("https://azul.example/")


def test_reindexing_blocks_when_not_up(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests, "get", lambda *a, **k: _FakeResp(json_data=_progress(up=False))
    )
    with pytest.raises(AzulReindexingError, match="up=false"):
        assert_azul_not_reindexing("https://azul.example/")


# --- validate_azul_token (tokeninfo mocked) ---


def test_validate_ok(monkeypatch):
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info())
    info = validate_azul_token("tok", expected_azp=_CLIENT_ID, allowed_emails=[_EMAIL])
    assert info["azp"] == _CLIENT_ID


def test_validate_allows_any_of_multiple(monkeypatch):
    # Multi-operator: a token from any account in allowed_emails passes.
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info(email="second@x.org"))
    info = validate_azul_token(
        "tok", expected_azp=_CLIENT_ID, allowed_emails=["first@x.org", "second@x.org"]
    )
    assert info["email"] == "second@x.org"


def test_validate_azp_mismatch(monkeypatch):
    monkeypatch.setattr(
        azul_auth, "tokeninfo", lambda t: _info(azp="999-x.apps.googleusercontent.com")
    )
    with pytest.raises(AzulTokenError, match="azp"):
        validate_azul_token("tok", expected_azp=_CLIENT_ID)


def test_validate_email_not_authorized(monkeypatch):
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info(email="someone@else.org"))
    with pytest.raises(AzulTokenError):
        validate_azul_token("tok", expected_azp=_CLIENT_ID, allowed_emails=[_EMAIL])


def test_validate_rejects_when_not_in_multiple(monkeypatch):
    # Multi-operator: a token from an account in NONE of allowed_emails is rejected, and the check
    # is case-insensitive on the allow-set side too (mixed-case entries still match exactly).
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info(email="outsider@x.org"))
    with pytest.raises(AzulTokenError):
        validate_azul_token(
            "tok", expected_azp=_CLIENT_ID, allowed_emails=["First@x.org", "Second@x.org"]
        )


def test_validate_empty_allowed_emails_fails_closed(monkeypatch):
    # A provided-but-empty allow-set rejects every account (fail-closed, matching the identity guard);
    # only allowed_emails=None skips the email check.
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info(email=_EMAIL))
    with pytest.raises(AzulTokenError):
        validate_azul_token("tok", expected_azp=_CLIENT_ID, allowed_emails=[])


def test_validate_expired(monkeypatch):
    monkeypatch.setattr(azul_auth, "tokeninfo", lambda t: _info(expires_in="60"))
    with pytest.raises(AzulTokenError, match="expires"):
        validate_azul_token("tok", expected_azp=_CLIENT_ID, min_remaining_seconds=300)


# --- read / write token file ---


def test_read_token_unset_returns_none():
    assert read_azul_token(_tier(token_file=None)) is None


def test_read_token_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_azul_token(_tier(token_file=str(tmp_path / "nope.txt")))


def test_read_token_empty_or_comments_returns_none(tmp_path):
    f = tmp_path / "tok.txt"
    f.write_text("# only comments\n\n")
    assert read_azul_token(_tier(token_file=str(f))) is None


def test_token_from_file_text_skips_comment_header():
    text = (
        "# Azul access token for tier 'prod' — SECRET, DO NOT COMMIT.\n"
        "# account: sa-user@broadinstitute.org   azp: 111.apps.googleusercontent.com\n"
        "\n"
        "the-token-value\n"  # placeholder (not a real ya29.* token) so git-secrets doesn't flag it
    )
    assert token_from_file_text(text) == "the-token-value"
    assert token_from_file_text("# only comments\n\n") is None
    assert token_from_file_text("") is None


def test_write_then_read_roundtrip(tmp_path):
    f = tmp_path / "secrets" / "azul-token-dev.txt"
    tier = _tier(token_file=str(f))
    path = write_azul_token(tier, "the-access-token-value", _info())
    assert path == f
    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.read_text().startswith("#")  # human-readable header
    assert read_azul_token(tier) == "the-access-token-value"


def test_write_token_no_file_configured_raises():
    with pytest.raises(AzulTokenError):
        write_azul_token(_tier(token_file=None), "tok", _info())


# --- expected_client_id ---


def test_expected_client_id_prefers_config():
    assert expected_client_id(_tier(client_id=_CLIENT_ID)) == _CLIENT_ID


def test_expected_client_id_discovers(monkeypatch):
    monkeypatch.setattr(
        azul_auth.requests, "get", lambda *a, **k: _FakeResp(f'const client_id = "{_CLIENT_ID}";')
    )
    assert expected_client_id(_tier(client_id=None)) == _CLIENT_ID
