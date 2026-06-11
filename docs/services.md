# Service Registry

Canonical reference for the external services this project integrates with. **This is the single
source of truth** for repos, OpenAPI/Swagger spec locations, and per-tier deployment URLs — the
tier-aware config should be derived from this table, not by hardcoding URLs elsewhere.

All spec URLs and deployment hosts below were verified reachable (HTTP 200) on 2026-06-09.

## Tiers

Every service exists in two independent tiers — **dev** and **prod** — and the full
ADSW→TDR→ADE→AW flow works end-to-end within each tier. Develop against **dev**; promote to
**prod** once solid. Never mix tiers within a single run.

The tier→URL mapping is **not** a pure string substitution:
- The Broad DSDE services (Firecloud, Rawls, Sam) mostly follow `dsde-dev` ↔ `dsde-prod`.
- **Firecloud prod** (`api.firecloud.org`) and **TDR prod** (`data.terra.bio`) use vanity hostnames.
- **Azul**'s two tiers are on entirely different domains.

So the config needs an explicit per-service, per-tier URL table (below) rather than substitution.

## Services

### AnVIL Data Explorer — Azul service
- **Role:** AnVIL-wide indexing service; PFB export of dataset tabular data + GA4GH DRS references.
- **Repo:** https://github.com/DataBiosphere/azul
- **Spec (OpenAPI 3.0.1):** https://raw.githubusercontent.com/DataBiosphere/azul/refs/heads/develop/lambdas/service/openapi.json
- **Dev:** https://service.anvil.gi.ucsc.edu/
- **Prod:** https://service.explore.anvilproject.org/

### Terra Firecloud Orchestration
- **Role:** Terra orchestration layer (fronts Rawls et al. for workspace operations).
- **Repo:** https://github.com/broadinstitute/firecloud-orchestration
- **Spec (OpenAPI 3.0.1):** https://raw.githubusercontent.com/broadinstitute/firecloud-orchestration/refs/heads/develop/src/main/resources/swagger/api-docs.yaml
- **Dev:** https://firecloud-orchestration.dsde-dev.broadinstitute.org/
- **Prod:** https://api.firecloud.org/

### Terra Rawls
- **Role:** Terra workspace + data-table (entity) backend; source for the Analysis Workspace tables.
- **Repo:** https://github.com/broadinstitute/rawls
- **Spec (OpenAPI 3.0.1):** https://raw.githubusercontent.com/broadinstitute/rawls/refs/heads/develop/core/src/main/resources/swagger/api-docs.yaml
  - ⚠️ Spec path is `core/src/main/...` — Rawls differs from Sam (see below).
- **Dev:** https://rawls.dsde-dev.broadinstitute.org/
- **Prod:** https://rawls.dsde-prod.broadinstitute.org/

### Terra Sam (auth)
- **Role:** Terra identity / authorization (tokens, resource access). Auth backbone the other
  Terra services depend on; not shown in the ADSW→TDR→ADE→AW data-flow diagram.
- **Repo:** https://github.com/broadinstitute/sam
- **Spec (OpenAPI 3.0.1):** https://raw.githubusercontent.com/broadinstitute/sam/refs/heads/develop/src/main/resources/swagger/api-docs.yaml
  - ⚠️ Spec path is `src/main/...` (**no** `core/` prefix). Do not copy Rawls's `core/` here.
- **Dev:** https://sam.dsde-dev.broadinstitute.org/
- **Prod:** https://sam.dsde-prod.broadinstitute.org/

### Terra Data Repository (TDR / jade-data-repo)
- **Role:** Dataset Datasets/Snapshots; the **source of truth** for QC comparisons. Tabular data
  queried via BigQuery (SQL).
- **Repo:** https://github.com/DataBiosphere/jade-data-repo
- **Spec (OpenAPI 3.0.1):** https://raw.githubusercontent.com/DataBiosphere/jade-data-repo/refs/heads/develop/src/main/resources/api/data-repository-openapi.yaml
- **Dev:** https://jade.datarepo-dev.broadinstitute.org/
- **Prod:** https://data.terra.bio/

## Quick reference table

| Service | Tier | Deployment URL |
|---|---|---|
| Azul | dev | https://service.anvil.gi.ucsc.edu/ |
| Azul | prod | https://service.explore.anvilproject.org/ |
| Firecloud Orchestration | dev | https://firecloud-orchestration.dsde-dev.broadinstitute.org/ |
| Firecloud Orchestration | prod | https://api.firecloud.org/ |
| Rawls | dev | https://rawls.dsde-dev.broadinstitute.org/ |
| Rawls | prod | https://rawls.dsde-prod.broadinstitute.org/ |
| Sam | dev | https://sam.dsde-dev.broadinstitute.org/ |
| Sam | prod | https://sam.dsde-prod.broadinstitute.org/ |
| TDR | dev | https://jade.datarepo-dev.broadinstitute.org/ |
| TDR | prod | https://data.terra.bio/ |

All Broad DSDE services expose a `/status` health endpoint.
