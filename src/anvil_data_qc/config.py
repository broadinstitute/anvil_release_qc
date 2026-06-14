"""Configuration loading and tier resolution.

Per-developer settings (Google identity, ADC credentials file, billing project) live in a YAML file
(gitignored ``config/config.yaml``; see ``config/config.example.yaml``). The shared, non-secret
service URLs are baked in here -- mirroring ``docs/services.md`` -- so they are not duplicated in
every developer's config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

# Per-tier service base URLs. Mirrors docs/services.md (the single source of truth for endpoints).
SERVICE_URLS: dict[str, dict[str, str]] = {
    "dev": {
        "azul": "https://service.anvil.gi.ucsc.edu/",
        "firecloud": "https://firecloud-orchestration.dsde-dev.broadinstitute.org/",
        "rawls": "https://rawls.dsde-dev.broadinstitute.org/",
        "sam": "https://sam.dsde-dev.broadinstitute.org/",
        "tdr": "https://jade.datarepo-dev.broadinstitute.org/",
    },
    "prod": {
        "azul": "https://service.explore.anvilproject.org/",
        "firecloud": "https://api.firecloud.org/",
        "rawls": "https://rawls.dsde-prod.broadinstitute.org/",
        "sam": "https://sam.dsde-prod.broadinstitute.org/",
        "tdr": "https://data.terra.bio/",
    },
}

TIERS: tuple[str, ...] = tuple(SERVICE_URLS.keys())


class TierConfig(BaseModel):
    """Per-developer settings for one tier."""

    email: str
    # None -> use the ambient ADC identity (e.g. a Terra workflow's runtime service account).
    adc_credentials_file: Optional[str] = None
    billing_project: str
    # File holding this tier's Azul access token (gitignored secret); populated by `azul-login`.
    azul_token_file: Optional[str] = None
    # The Azul OAuth client id a token must carry as `azp`. Optional: auto-discovered from Azul's
    # served Swagger initializer when unset (so prod's id need not be hardcoded).
    azul_oauth2_client_id: Optional[str] = None


class AppConfig(BaseModel):
    default_tier: str = "dev"
    tiers: dict[str, TierConfig]


class ResolvedTier(BaseModel):
    """A tier's per-developer config combined with its (baked-in) service URLs."""

    name: str
    email: str
    adc_credentials_file: Optional[str]
    billing_project: str
    azul_token_file: Optional[str]
    azul_oauth2_client_id: Optional[str] = None
    service_urls: dict[str, str]

    def url(self, service: str) -> str:
        return self.service_urls[service]


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}. Copy config/config.example.yaml to {path} and fill it in."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return AppConfig.model_validate(data)


def resolve_tier(config: AppConfig, tier_name: Optional[str] = None) -> ResolvedTier:
    name = tier_name or config.default_tier
    if name not in SERVICE_URLS:
        raise ValueError(f"Unknown tier '{name}'. Known tiers: {', '.join(TIERS)}.")
    if name not in config.tiers:
        raise ValueError(f"Tier '{name}' is not configured in your config file.")
    tc = config.tiers[name]
    return ResolvedTier(
        name=name,
        email=tc.email,
        adc_credentials_file=tc.adc_credentials_file,
        billing_project=tc.billing_project,
        azul_token_file=tc.azul_token_file,
        azul_oauth2_client_id=tc.azul_oauth2_client_id,
        service_urls=SERVICE_URLS[name],
    )
