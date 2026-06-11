"""Tests for APAT reading (no network)."""

import pytest

from anvil_data_qc.auth import read_azul_apat
from anvil_data_qc.config import ResolvedTier


def _tier(apat_file):
    return ResolvedTier(
        name="dev",
        email="u@test.firecloud.org",
        adc_credentials_file=None,
        billing_project="b",
        azul_apat_file=apat_file,
        service_urls={},
    )


def test_read_apat_raw_token(tmp_path):
    f = tmp_path / "apat.txt"
    f.write_text("# instructions\n# more\n\neyJraw.token.value\n")
    assert read_azul_apat(_tier(str(f))) == "eyJraw.token.value"


def test_read_apat_json_response(tmp_path):
    f = tmp_path / "apat.txt"
    f.write_text('# comment\n{"token": "eyJjson.token.value"}\n')
    assert read_azul_apat(_tier(str(f))) == "eyJjson.token.value"


def test_read_apat_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_azul_apat(_tier(str(tmp_path / "nope.txt")))


def test_read_apat_not_configured_returns_none():
    assert read_azul_apat(_tier(None)) is None


def test_read_apat_empty_or_commented_returns_none(tmp_path):
    f = tmp_path / "apat.txt"
    f.write_text("# only comments, no token\n\n")  # commented-out / empty -> anonymous
    assert read_azul_apat(_tier(str(f))) is None


def test_read_apat_rejects_non_jwt(tmp_path):
    f = tmp_path / "apat.txt"
    f.write_text("ya29.this-is-an-access-token-not-a-jwt\n")  # wrong token type / shape
    with pytest.raises(ValueError):
        read_azul_apat(_tier(str(f)))
