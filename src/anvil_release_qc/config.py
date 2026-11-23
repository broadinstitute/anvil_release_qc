"""Settings and tier resolution.

Every setting has a source that does not need a file: the two billing values come from CLI flags (the
WDLs pass them as optional inputs) or fall back to the built-in prod defaults below, and in a Terra
workflow credentials are the ambient runtime identity. The shared, non-secret service URLs are baked in
here -- mirroring ``docs/services.md``.

An **optional** local YAML file (gitignored ``config/config.yaml``; see ``config/config.example.yaml``)
remains for local convenience -- per-tier saved ADC file, Azul token file, the batch runner's shared QC
workspace, and billing overrides. It is never used by the WDLs (the Docker image excludes ``config/``).
Precedence for the billing values: CLI flag > config file > built-in tier default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Optional

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

#: Tier used when neither --tier nor a config file's default_tier says otherwise.
DEFAULT_TIER = "prod"

#: Built-in billing defaults per tier (prod only -- the shared AnVIL QC billing). Dev has none, so a dev
#: run must name its own billing values (flag or config) rather than silently borrowing prod's.
DEFAULT_BILLING: dict[str, dict[str, str]] = {
    "prod": {
        "terra_billing_project": "anvil-developer-work",
        "billing_google_project_id": "terra-ec38e08d",
    },
}

#: The two billing settings, and the CLI flag that sets each.
BILLING_FIELDS: tuple[str, ...] = ("terra_billing_project", "billing_google_project_id")
_FLAG = {
    "terra_billing_project": "--billing-project",
    "billing_google_project_id": "--billing-google-project-id",
}

#: Local config file loaded automatically when present and no --config is given.
DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class TierConfig(BaseModel):
    """Optional local settings for one tier (every field may be omitted)."""

    # None -> use the ambient ADC identity (e.g. a Terra workflow's runtime service account).
    adc_credentials_file: Optional[str] = None
    # The Terra billing project (a.k.a. workspace namespace) that ade-qc/tdr-qc create workspaces under.
    # Unset -> the --billing-project flag, else the tier's built-in default.
    terra_billing_project: Optional[str] = None
    # The Google Cloud Project ID to run/bill Google Cloud operations against -- BigQuery jobs now, and
    # requester-pays access later. This is a real GCP project id (the Terra Workspace Google Project ID,
    # e.g. terra-xxxxxxxx), NOT the Terra billing project. Unset -> the --billing-google-project-id flag,
    # else the tier's built-in default.
    billing_google_project_id: Optional[str] = None
    # The existing shared QC workspace (e.g. anvil-data-qc-shared) that qc-batch-runner submits into.
    # Optional here -- only the batch runner needs it (and it also accepts a CLI override); the ade-qc/
    # tdr-qc/compare/compare-ingest commands do not use it.
    qc_workspace_name: Optional[str] = None
    # The Azul OAuth client id a token must carry as `azp`. Optional: auto-discovered from Azul's
    # served Swagger initializer when unset (so prod's id need not be hardcoded).
    azul_oauth2_client_id: Optional[str] = None


class AppConfig(BaseModel):
    # Unknown keys (e.g. the retired ``email`` / ``authorized_users`` / ``azul_token_file``) are
    # ignored, so old files load.
    # A config file without default_tier keeps meaning dev (unchanged); with no file at all the tier
    # defaults to DEFAULT_TIER (prod).
    default_tier: str = "dev"
    tiers: dict[str, TierConfig] = {}


class ResolvedTier(BaseModel):
    """A tier's resolved settings combined with its (baked-in) service URLs."""

    name: str
    adc_credentials_file: Optional[str]
    # Set whenever the command requires it (see resolve_tier's ``require``).
    terra_billing_project: Optional[str]
    billing_google_project_id: Optional[str]
    qc_workspace_name: Optional[str] = None
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


def load_optional_config(path: Optional[Path] = None) -> Optional[AppConfig]:
    """Load the local config file if there is one.

    An explicit ``path`` must exist. With no path, ``config/config.yaml`` is loaded when present and
    otherwise there is simply no config (the normal case in a WDL run).
    """
    if path is not None:
        return load_config(path)
    return load_config(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else None


def resolve_tier(
    config: Optional[AppConfig] = None,
    tier_name: Optional[str] = None,
    *,
    terra_billing_project: Optional[str] = None,
    billing_google_project_id: Optional[str] = None,
    require: Collection[str] = BILLING_FIELDS,
) -> ResolvedTier:
    """Resolve one tier's settings: CLI value > config file > built-in tier default.

    Raises ``ValueError`` when a field in ``require`` has no source (e.g. a dev run that names neither
    flag nor config value -- dev has no built-in defaults). Commands pass only what they use: ``compare``
    needs just ``billing_google_project_id``; ``azul-login`` needs neither.
    """
    name = tier_name or (config.default_tier if config else DEFAULT_TIER)
    if name not in SERVICE_URLS:
        raise ValueError(f"Unknown tier '{name}'. Known tiers: {', '.join(TIERS)}.")
    tc = (config.tiers.get(name) if config else None) or TierConfig()
    defaults = DEFAULT_BILLING.get(name, {})

    billing_project = (
        terra_billing_project or tc.terra_billing_project or defaults.get("terra_billing_project")
    )
    google_project = (
        billing_google_project_id
        or tc.billing_google_project_id
        or defaults.get("billing_google_project_id")
    )
    resolved = {
        "terra_billing_project": billing_project,
        "billing_google_project_id": google_project,
    }
    missing = [f for f in require if not resolved[f]]
    if missing:
        flags = ", ".join(_FLAG[f] for f in missing)
        raise ValueError(
            f"Tier '{name}' has no built-in default for {', '.join(missing)}: pass {flags} "
            "(or set it for the tier in the local config/config.yaml)."
        )
    return ResolvedTier(
        name=name,
        adc_credentials_file=tc.adc_credentials_file,
        terra_billing_project=billing_project,
        billing_google_project_id=google_project,
        qc_workspace_name=tc.qc_workspace_name,
        azul_oauth2_client_id=tc.azul_oauth2_client_id,
        service_urls=SERVICE_URLS[name],
    )


def resolve_settings(
    config_path: Optional[Path] = None,
    tier_name: Optional[str] = None,
    *,
    terra_billing_project: Optional[str] = None,
    billing_google_project_id: Optional[str] = None,
    require: Collection[str] = BILLING_FIELDS,
) -> ResolvedTier:
    """Load the optional local config (see ``load_optional_config``) and resolve the tier from it."""
    return resolve_tier(
        load_optional_config(config_path),
        tier_name,
        terra_billing_project=terra_billing_project,
        billing_google_project_id=billing_google_project_id,
        require=require,
    )
