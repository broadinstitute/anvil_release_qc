"""Tests for settings resolution: optional local config, CLI overrides, built-in defaults (no network)."""

from pathlib import Path

import pytest

from anvil_release_qc import config as config_mod
from anvil_release_qc.config import (
    DEFAULT_BILLING,
    SERVICE_URLS,
    load_config,
    load_optional_config,
    resolve_settings,
    resolve_tier,
)

PROD_BILLING = "anvil-developer-work"
PROD_GOOGLE = "terra-ec38e08d"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_prod_defaults_are_the_shared_qc_billing():
    assert DEFAULT_BILLING["prod"] == {
        "terra_billing_project": PROD_BILLING,
        "billing_google_project_id": PROD_GOOGLE,
    }


def test_no_config_defaults_to_prod_with_builtin_billing():
    tier = resolve_tier(None, None)
    assert tier.name == "prod"
    assert tier.terra_billing_project == PROD_BILLING
    assert tier.billing_google_project_id == PROD_GOOGLE
    assert tier.adc_credentials_file is None  # ambient ADC (the WDL case)
    assert tier.service_urls == SERVICE_URLS["prod"]


def test_cli_values_override_defaults():
    tier = resolve_tier(
        None, "prod", terra_billing_project="other-bp", billing_google_project_id="terra-other"
    )
    assert tier.terra_billing_project == "other-bp"
    assert tier.billing_google_project_id == "terra-other"


def test_dev_has_no_builtin_billing_and_says_which_flag(tmp_path):
    # Dev must never silently borrow prod's billing values.
    with pytest.raises(ValueError, match="--billing-project, --billing-google-project-id"):
        resolve_tier(None, "dev")
    tier = resolve_tier(
        None, "dev", terra_billing_project="dev-bp", billing_google_project_id="terra-dev"
    )
    assert tier.terra_billing_project == "dev-bp"


def test_require_only_what_the_command_uses():
    # compare / compare-ingest need only the Google project; azul-login needs neither.
    tier = resolve_tier(
        None, "dev", billing_google_project_id="terra-dev", require=("billing_google_project_id",)
    )
    assert tier.terra_billing_project is None
    assert resolve_tier(None, "dev", require=()).billing_google_project_id is None


def test_config_file_values_and_precedence(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
default_tier: dev
tiers:
  dev:
    adc_credentials_file: ~/.config/anvil-release-qc/adc-dev.json
    terra_billing_project: my-dev-billing
    billing_google_project_id: terra-dev-1234
  prod:
    qc_workspace_name: anvil-data-qc-shared
""",
    )
    config = load_config(cfg_path)

    dev = resolve_tier(config, None)  # falls back to the file's default_tier
    assert dev.name == "dev"
    assert dev.terra_billing_project == "my-dev-billing"
    assert dev.billing_google_project_id == "terra-dev-1234"

    # CLI flag beats the config file.
    assert resolve_tier(config, "dev", terra_billing_project="flag-bp").terra_billing_project == (
        "flag-bp"
    )

    # A tier listed without billing values falls back to the built-in default.
    prod = resolve_tier(config, "prod")
    assert prod.terra_billing_project == PROD_BILLING
    assert prod.qc_workspace_name == "anvil-data-qc-shared"


def test_config_file_without_default_tier_still_means_dev(tmp_path):
    cfg_path = _write(tmp_path, "tiers: {}\n")
    with pytest.raises(ValueError):  # dev, and dev has no built-in billing
        resolve_tier(load_config(cfg_path), None)


def test_retired_email_fields_are_ignored(tmp_path):
    # Old local configs with email / authorized_users still load.
    cfg_path = _write(
        tmp_path,
        """
tiers:
  prod:
    email: someone@broadinstitute.org
    authorized_users: [teammate@broadinstitute.org]
""",
    )
    tier = resolve_tier(load_config(cfg_path), "prod")
    assert not hasattr(tier, "email")
    assert tier.terra_billing_project == PROD_BILLING


def test_unknown_tier_raises():
    with pytest.raises(ValueError):
        resolve_tier(None, "staging")


def test_explicit_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_optional_config(tmp_path / "does-not-exist.yaml")


def test_default_config_path_is_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    assert load_optional_config(None) is None  # absent -> no config (the WDL case)
    resolved = resolve_settings(None, None)
    assert resolved.terra_billing_project == PROD_BILLING

    _write(tmp_path, "default_tier: prod\ntiers:\n  prod:\n    terra_billing_project: from-file\n")
    assert resolve_settings(None, None).terra_billing_project == "from-file"  # auto-loaded
