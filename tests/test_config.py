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
    billing_project: my-dev-billing
  prod:
    email: prod-user@example.org
    billing_project: my-prod-billing
""",
    )
    config = load_config(cfg_path)
    assert config.default_tier == "dev"

    dev = resolve_tier(config, None)  # falls back to default_tier
    assert dev.name == "dev"
    assert dev.email == "dev-user@test.firecloud.org"
    assert dev.billing_project == "my-dev-billing"
    assert dev.service_urls == SERVICE_URLS["dev"]

    prod = resolve_tier(config, "prod")
    assert prod.adc_credentials_file is None  # omitted -> ambient ADC (deployment)
    assert prod.url("rawls").startswith("https://rawls.dsde-prod")


def test_unknown_tier_raises(tmp_path):
    cfg_path = _write(
        tmp_path,
        """
tiers:
  dev:
    email: x@test.firecloud.org
    billing_project: b
""",
    )
    config = load_config(cfg_path)
    with pytest.raises(ValueError):
        resolve_tier(config, "staging")


def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")
