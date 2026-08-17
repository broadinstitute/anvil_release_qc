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
    # The Terra billing project (a.k.a. workspace namespace) that ade-qc/tdr-qc create workspaces under.
    terra_billing_project: str
    # The Google Cloud Project ID to run/bill Google Cloud operations against -- BigQuery jobs now, and
    # requester-pays access later. This is a real GCP project id (the Terra Workspace Google Project ID,
    # e.g. terra-xxxxxxxx), NOT the Terra billing project. See config.example.yaml.
    billing_google_project_id: str
    # The existing shared QC workspace (e.g. anvil-data-qc-shared) that qc-batch-runner submits into.
    # Optional here -- only the batch runner needs it (and it also accepts a CLI override); the ade-qc/
    # tdr-qc/compare/compare-ingest commands do not use it.
    qc_workspace_name: Optional[str] = None
    # File holding this tier's Azul access token (gitignored secret); populated by `azul-login`.
    azul_token_file: Optional[str] = None
    # The Azul OAuth client id a token must carry as `azp`. Optional: auto-discovered from Azul's
    # served Swagger initializer when unset (so prod's id need not be hardcoded).
    azul_oauth2_client_id: Optional[str] = None
    # Additional human Terra users authorized to run this tier (multi-operator). The identity guard and
    # the Azul-token check accept any account in {email} ∪ authorized_users.
    authorized_users: list[str] = []


class AppConfig(BaseModel):
    default_tier: str = "dev"
    tiers: dict[str, TierConfig]


class ResolvedTier(BaseModel):
    """A tier's per-developer config combined with its (baked-in) service URLs."""

    name: str
    email: str
    adc_credentials_file: Optional[str]
    terra_billing_project: str
    billing_google_project_id: str
    qc_workspace_name: Optional[str] = None
    azul_token_file: Optional[str]
    azul_oauth2_client_id: Optional[str] = None
    authorized_users: list[str] = []
    service_urls: dict[str, str]

    def url(self, service: str) -> str:
        return self.service_urls[service]

    @property
    def authorized_emails(self) -> set[str]:
        """Human Terra users allowed to run this tier (lowercased): ``{email}`` ∪ ``authorized_users``."""
        return {e.strip().lower() for e in [self.email, *self.authorized_users] if e and e.strip()}


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
        terra_billing_project=tc.terra_billing_project,
        billing_google_project_id=tc.billing_google_project_id,
        qc_workspace_name=tc.qc_workspace_name,
        azul_token_file=tc.azul_token_file,
        azul_oauth2_client_id=tc.azul_oauth2_client_id,
        authorized_users=tc.authorized_users,
        service_urls=SERVICE_URLS[name],
    )
