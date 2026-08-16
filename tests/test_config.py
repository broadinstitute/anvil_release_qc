"""Tests for config loading and tier resolution (no network required)."""

from pathlib import Path

import pytest

from anvil_data_qc.config import SERVICE_URLS, load_config, resolve_tier


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_load_and_resolve_tier(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
default_tier: dev
tiers:
  dev:
    email: dev-user@test.firecloud.org
    adc_credentials_file: ~/.config/anvil-data-qc/adc-dev.json
    terra_billing_project: my-dev-billing
    billing_google_project_id: terra-dev-1234
  prod:
    email: prod-user@example.org
    terra_billing_project: my-prod-billing
    billing_google_project_id: terra-prod-5678
""",
    )
    config = load_config(cfg_path)
    assert config.default_tier == "dev"

    dev = resolve_tier(config, None)  # falls back to default_tier
    assert dev.name == "dev"
    assert dev.email == "dev-user@test.firecloud.org"
    assert dev.terra_billing_project == "my-dev-billing"
    assert dev.billing_google_project_id == "terra-dev-1234"
    assert dev.service_urls == SERVICE_URLS["dev"]

    prod = resolve_tier(config, "prod")
    assert prod.adc_credentials_file is None  # omitted -> ambient ADC (deployment)
    assert prod.terra_billing_project == "my-prod-billing"
    assert prod.billing_google_project_id == "terra-prod-5678"
    assert prod.url("rawls").startswith("https://rawls.dsde-prod")


def test_authorized_emails_default_is_just_email(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
tiers:
  dev:
    email: Dev-User@test.firecloud.org
    terra_billing_project: b
    billing_google_project_id: g
""",
    )
    tier = resolve_tier(load_config(cfg_path), "dev")
    assert tier.authorized_users == []
    assert tier.authorized_emails == {"dev-user@test.firecloud.org"}  # lowercased, = {email}


def test_authorized_emails_union_with_authorized_users(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
tiers:
  dev:
    email: primary@broadinstitute.org
    terra_billing_project: b
    billing_google_project_id: g
    authorized_users:
      - Teammate@broadinstitute.org
      - ops@broadinstitute.org
""",
    )
    tier = resolve_tier(load_config(cfg_path), "dev")
    assert tier.authorized_emails == {
        "primary@broadinstitute.org",
        "teammate@broadinstitute.org",
        "ops@broadinstitute.org",
    }


def test_unknown_tier_raises(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
tiers:
  dev:
    email: x@test.firecloud.org
    terra_billing_project: b
    billing_google_project_id: g
""",
    )
    config = load_config(cfg_path)
    with pytest.raises(ValueError):
        resolve_tier(config, "staging")


def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")


@pytest.mark.parametrize("missing", ["terra_billing_project", "billing_google_project_id"])
def test_required_project_fields_fail_fast(tmp_path, missing):
    # Both project fields are required (no silent fallback) -- omitting either fails at load, so a
    # misconfigured tier is caught up front rather than mid-run. See the terra-billing vs google-project split.
    fields = {
        "email": "x@test.firecloud.org",
        "terra_billing_project": "b",
        "billing_google_project_id": "terra-x",
    }
    del fields[missing]
    body = "\n".join(f"    {k}: {v}" for k, v in fields.items())
    cfg_path = _write(tmp_path, f"tiers:\n  dev:\n{body}\n")
    with pytest.raises(ValueError):
        load_config(cfg_path)
