# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

`anvil-release-qc` is **built and in production use** (deployed image `:0.3.4`; repo now at `0.3.5`,
pending publish). The package
`src/anvil_release_qc/` provides: `config` (YAML + pydantic, tier resolution), `auth` (ADC per tier +
Sam-resolved identity guard), `azul_auth` (Azul token capture/validation), `logging_setup` + `redaction`
(structured, secret-redacted request/response logging), `timing` (per-stage durations), `clients/` (base
+ `tdr`, `azul`, `rawls`, `firecloud`, `sam`), `bigquery` (Azul `dataset_id` resolution + snapshot
**data-volume metrics**), `workspace` (naming), `compare/` (the comparison seams: `sources`, `profile`,
`engine`, `report`, `authdomain`, `qc`, `ingest`), `safety` (the 4-layer controlled-access defense),
`pipeline` (the wired flows), and `cli` (Typer: `ade-qc`, `tdr-qc`, `compare`, `compare-ingest`,
`azul-login`).

**Four operations — two export routes share one QC tail:**
- **`ade-qc`** — ADE/Azul export: TDR → BigQuery `dataset_id` → Azul PFB manifest → Rawls
  `createWorkspace` → Firecloud `importJob` (`filetype: pfb`) → **QC**.
- **`tdr-qc`** ([`docs/tdr_export_flow.md`](docs/tdr_export_flow.md)) — a **direct TDR snapshot export**
  (no ADE/Azul; Terra/ADC tokens throughout) → signed `manifest.json` → the **same** import tail
  (`importJob` `filetype: tdrexport`, `tdrSyncPermissions: false`) → the **same** QC — for very large
  datasets that fail ADE export.
- **`compare`** — re-run the QC on an existing workspace, no import.
- **`compare-ingest`** — the upstream `ADSW → TDR` ingestion-completeness check (submitter workspace vs
  TDR snapshot; table presence + exact row-count parity, driven from the submitter side).

The two export flows share `pipeline._create_import_and_qc` (the import+QC tail). **QC** = AW-vs-snapshot
table comparison (T1–T2: table parity, row counts, column-count sanity) + auth-domain verification.

**Cross-cutting capabilities:**
- **Controlled-access** via the default-off `--allow-controlled-access` opt-in on both `ade-qc` and
  `tdr-qc` (Broad security-team approved 2026-06 — no restrictions, all AnVIL data). `ade-qc` also needs
  an Azul token; `tdr-qc` needs none (the operator's own Terra creds must grant the access). See
  `safety.py` and risk **R8**; keep the opt-in **default-off** and do not weaken layers 2–4.
- **`--verify-auth`** — a fast, side-effect-free preflight (identity guard → Azul token (ade) →
  retrieveSnapshot → consent gate → exit **before** any export/signed URL).
- **Azul reindexing preflight** (`ade-qc`; v0.3.5) — before the export, `azul_auth.assert_azul_not_reindexing`
  reads Azul's `/health/progress`; any non-zero unindexed backlog (a catalog (re)index in progress) **fails
  fast** (exit 2, retryable) rather than exporting an empty/incomplete manifest and producing a false QC
  verdict. Strict-zero; fail-open on a health-endpoint blip. Also in `qc-batch-runner --verify-auth`. See
  [`docs/qc_batch_runner.md`](docs/qc_batch_runner.md) → *Azul reindexing*.
- **Data-volume metrics** (`total_table_count` / `total_row_count` / `total_value_count`) and **per-stage
  durations** (minutes) — computed early and surfaced even on a failed/timed-out import, and exposed as
  WDL outputs; instruments for the Terra Data Table capacity question (below).
- **Run metadata as WDL outputs / data-table columns** (v0.3.5) — **run timestamps** (`run_began` /
  `run_ended`, UTC ISO-8601 to the second, all four workflows) and, for `ade-qc`/`tdr-qc`, the **created
  workspace** (`workspace_namespace` / `workspace_name`) — so results sort by run time, correlate to a
  UCSC reindex/fix, and the retained test workspace is findable for a later `compare` / cross-operator
  access / cleanup.

For deployment, a code-only **Docker image** (`Dockerfile`) plus four **womtool-validated WDL** wrappers
(`wdl/{ade_qc,tdr_qc,compare,compare_ingest}.wdl`) run the same CLI non-interactively in Terra. Each WDL
has two committed config sets — a safe `*.inputs.example.json` and the portable **production**
`*.inputs.json` + `*.outputs.json` (secrets abstracted to Terra Workspace Data). See
[`wdl/README.md`](wdl/README.md).

**Status (through v0.3.1 — deployed and in production use):**
- ✅ Unit tests pass (`pytest`, mocked HTTP) and `ruff` is clean.
- ✅ **Open-access** validated end-to-end on **dev and prod** (1000G): `compare` reports QC PASS
  (12 tables; row + column-count parity; empty TDR tables and `datarepo_row_ids` excluded; auth-domain
  match).
- ✅ **Controlled-access** validated end-to-end: `ade-qc` against a **real non-empty auth domain**
  (2026-06-26, the `ANVIL_ccdg_asc_ndd_daly_talkowski_cdcseed_asd_…` snapshot → QC PASS, auth-domain
  match — the first exercise of the auth-domain check against a non-empty domain), and `tdr-qc` in
  **prod** (2026-06-27, the `ANVIL_dGTEx_GRU_v1` GRU snapshot → QC PASS, `AUTH_AnVIL_dGTEx_GRU_v1` on
  both sides). Proved out the Sam-resolved identity guard and the multi-user `authorized_users` allowlist.
- ✅ **Prod batch** run: the open-access anvil14 snapshots via `ade-qc`, and 8 large clinical/EHR datasets
  via `tdr-qc`.
- ⚠️ **Terra Data Table import ceiling discovered:** the largest clinical/EHR dataset (17 tables /
  ~13.1M rows / ~142M values) **failed to import** even after ~11h42m — Terra's import service has a ~6 h
  job TTL enforced by a periodic reaper (effective max ~6–12 h), and this dataset exceeds it. The import
  is transactional (empty tables on failure); the data-volume metrics quantify the scale, and
  `compare-ingest` (no import) still works for such datasets. See risk **R4** and
  [`docs/comparison.md`](docs/comparison.md).
- ⏭️ **Deferred QC depth** (see [`docs/comparison.md`](docs/comparison.md)): full column-name
  comparison, T3 id-set parity, T4 cell-value comparison.
- 🆕 **v0.3.5 (this release, pending publish):** the Azul **reindexing preflight** (guards `ade-qc`
  against QC'ing an empty/incomplete manifest during a UCSC catalog reindex); **run timestamps** and the
  **created-workspace** namespace/name as WDL outputs / data-table columns (both described above). Bundled
  into one image publish.

Environment: Python **3.13.14** (pyenv; pinned in `.python-version`), local `.venv`, deps from
`pyproject.toml` with pinned `requirements.txt` / `requirements-dev.txt` (pip-tools). See the README
for developer setup, and [`docs/risk_register.md`](docs/risk_register.md) for tracked risks.

## What this project does

`anvil-release-qc` provides automated **quality control of AnVIL data releases**. AnVIL hosts
hundreds of biomedical datasets, each combining relational tabular data with cloud file blobs.
As each dataset is released, this tooling verifies the release end-to-end. Because there are
hundreds of datasets, **thorough automation is the core requirement** — flows that are normally
performed interactively must be driven entirely through APIs.

## Domain architecture (the data-release pipeline)

A dataset flows through four AnVIL/Terra systems. Understanding these and their APIs is essential:

```
ADSW  ──ingestion──▶  TDR  ──indexing──▶  ADE  ──PFB export──▶  AW
```

- **ADSW** — AnVIL Data Submittal Workspace. A dataset-specific Terra workspace holding the
  submitted tabular + file data.
- **TDR** — Terra Data Repository. The dataset is ingested here as a **Dataset** and exposed via a
  **Snapshot**. The TDR snapshot is the **source of truth** for QC comparisons. Its tabular data is
  queried via **BigQuery (SQL)**.
- **ADE** — AnVIL Data Explorer. An AnVIL-wide indexing service (backed by the **Azul** service)
  with a researcher web UI. Exports dataset tabular data + GA4GH **DRS** references as **PFB**.
- **AW** — Analysis Workspace. A researcher's Terra workspace; receives the PFB export as data
  tables plus DRS references to cloud blobs.

**Sam** — Terra's identity/authorization service — underpins the Terra services above (tokens,
resource access) even though it isn't a stage in the data flow.

Relevant service APIs: the ADE **Azul** API; Terra's **Firecloud Orchestration**, **Rawls**, and
**Sam** APIs; and the **TDR (jade-data-repo)** API. Repos, OpenAPI spec URLs, and per-tier
deployment URLs for all of these are in **[`docs/services.md`](docs/services.md)** — treat that file
as the single source of truth for service endpoints.

### Two tiers: dev and prod

Every service above exists in two independent tiers — **dev** and **prod** — and the entire
ADSW→TDR→ADE→AW flow works end-to-end within each tier. Development is done against the **dev**
tier; once functionality is solid it is promoted to **prod**. Design implication: **tier is a
first-class config parameter**. Base URLs, credentials, and target endpoints for all services must
switch together based on tier, so moving from dev to prod is a configuration change, never a code
change. Never mix tiers within a single run.

## Primary QC functionality (initial scope)

For a single given dataset:

1. **Automate the ADE → AW export/import.** API-based export of a dataset from the AnVIL Data
   Explorer and API-based import into an Analysis Workspace. This flow is reliable but normally
   done interactively; automating it is the **biggest project risk and the first thing to derisk**.
   Aim for a working end-to-end path quickly, then refine. The automation should **mirror the
   interactive UI's API calls as closely as is reasonably possible** — captured real traffic from an
   interactive run (see [`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md)) is the
   reference of record, more reliable than inferring intent from the OpenAPI specs alone. **The flow
   is now resolved and documented in [`docs/export_import_flow.md`](docs/export_import_flow.md)** —
   actual endpoints, payloads, async polling, and auth-domain handling.
2. **Compare AW tables to the TDR snapshot.** The Analysis Workspace data tables (via Rawls/
   Firecloud) should match the source TDR snapshot (queried via BigQuery SQL) — same tables, same
   columns, same row counts, etc., allowing for expected differences.
3. **Verify the workspace authorization domain.** The controlled-access metadata must be protected:
   assert the workspace's auth domain(s) match the source snapshot's. The import service applies the
   auth domain automatically (a privileged op, *after* creation) — the automation **verifies** it,
   never sets it. An empty/mismatched domain on a controlled dataset is a QC failure.

The flow is **driven by the TDR snapshot id** (system of record); Azul's identifiers are cross-checks
only. **BigQuery is a core dependency** — used first to resolve the Azul `dataset_id` from the
snapshot's `anvil_dataset` table, then for the table comparison.

**Safety gate (fail-closed by default; controlled-access via explicit opt-in):** by default the
pipeline proceeds only when the snapshot's `consentCode` is exactly `NRES` (open-access); any other
value — including empty/unknown — is blocked **fail-closed, before any PFB signed URL is generated**. A
controlled-access signed URL grants direct access to an NIH study's controlled data; mishandling it is a
Federal DMI. **Controlled-access was reviewed and approved by the Broad security team (2026-06, no
restrictions — all AnVIL data)** and is enabled by an **explicit, default-off opt-in**:
`allow_controlled_access` (the `--allow-controlled-access` CLI flag / `ade_qc.wdl` input; see
`safety.assert_consent_permitted`), which permits any `consentCode` and emits an audit `WARNING`.
Controlled runs also require an Azul token (`ade_qc.wdl` `azul_token` input). The other three safety layers
(SignedUrl containment, hand-off verification, bearer-pinning) apply on every run. See
[`src/anvil_release_qc/safety.py`](src/anvil_release_qc/safety.py) and risk **R8**. Keep the opt-in
**default-off**, and do not weaken layers 2–4.

## Deployment / structure constraints (decide code layout with these in mind)

Two delivery modes must both be supported, which should drive how code is organized:

- **Interactive development via Jupyter notebooks** — the maintainer values the notebook-driven
  pattern used in the related `drs-test-notebooks` repo for development and troubleshooting.
- **Deployable as a Docker image run in Terra workflows via WDL** — the same functionality must run
  non-interactively in production.

The implication: keep core logic in an **importable, testable Python package** with a
**non-interactive entrypoint (CLI)**. Notebooks should be thin wrappers that import that package
rather than hosting logic themselves, so the identical code path runs in notebooks, in the Docker
image, and under WDL.

## Settled build decisions / conventions

The design is fully derisked; these decisions are settled and should guide the build. The end-to-end
flow itself is specified in [`docs/export_import_flow.md`](docs/export_import_flow.md).

**Stack & layout** (Python **3.13**):
- **pip + pip-tools** — deps declared in `pyproject.toml`, compiled to pinned `requirements.txt`
  (runtime) + `requirements-dev.txt` (dev); Docker installs from the pinned files.
- **PyYAML + pydantic v2** (config), **Typer** (CLI), **requests** (HTTP), **google-auth** (ADC →
  tokens + BigQuery), **google-cloud-bigquery** (a *core* dependency — see below), **ruff** + **pytest**
  (dev). (The comparison phase uses only the standard library — plain dicts/dataclasses, no pandas.)
- `src/`-layout package holding all logic; a non-interactive **CLI** entrypoint; **thin notebooks**
  that import the package (no logic); deployable as a **Docker image run via WDL** — identical code
  path in all three.

**Auth** (see *Two tiers*):
- Google **ADC** everywhere; **one or more authorized human Terra users per tier** (the tier `email`
  plus optional `authorized_users` — multi-operator), both tiers identical (in a Terra workflow the
  runtime **pet service account** resolves to its human user via Sam — see the identity guard).
- Locally, a per-tier saved ADC file selected via `GOOGLE_APPLICATION_CREDENTIALS`; in deployment,
  ADC falls back to the ambient runtime identity (no code change).
- The gcloud ADC creds serve **TDR, BigQuery, and Terra** (Rawls/Firecloud): one
  `google.auth.default()` for REST bearer tokens *and* the BigQuery client; tokens auto-refresh, so
  long ops survive the ~1 h expiry.
- **Azul needs its own token** — it rejects the gcloud ADC token (requires `azp` == Azul's OAuth
  client). Capture one interactively with `anvil-release-qc azul-login` (validated via Google `tokeninfo`,
  stored per tier in `azul_token_file`); no token ⇒ anonymous (open-access only). See
  [`export_import_flow.md`](docs/export_import_flow.md) → Authentication and risk **R7**.
- **Identity guard:** before any API call, resolve the active credentials to their **canonical Terra
  user** via Sam (`/api/users/v2/self`, which maps a Terra **pet service account** to its owning human
  user) and assert it is in the tier's **authorized set** (`email` ∪ `authorized_users`, supporting
  multiple operators); abort otherwise. So the configured accounts are **human Terra users** in every
  context (local *and* the Terra/WDL deployment, where the runtime identity is the pet SA) — the raw ADC
  identity is logged for the audit trail. The Azul-token check uses the same authorized set.
- Scopes: plain `gcloud auth application-default login` (defaults `openid`, `email`,
  `cloud-platform`) — no custom scope string.

**Config (YAML):** per-tier `{email, ADC file path, terra_billing_project, billing_google_project_id,
azul_token_file}` plus the shared service URLs (from [`services.md`](docs/services.md)). Committed
non-secret `config.example.yaml` + **gitignored** per-developer `config.yaml`. **Two distinct project
values** (they coincided only for some legacy billing projects, so keep them separate): `terra_billing_project`
= the Terra billing project (a.k.a. workspace namespace) that ade-qc/tdr-qc create workspaces under;
`billing_google_project_id` = a **real Google Cloud Project ID** (the Terra Workspace Google Project ID,
e.g. `terra-xxxxxxxx`) that **BigQuery jobs run in / are billed to** (and requester-pays later) — *not* the
Terra billing project. `check_bigquery_access` preflights it (also in `--verify-auth`).

**Workspace creation:** one **retained** workspace **per run**. Name =
`<user-local-part>_<infix>_<sanitized full snapshot name>_YYYYMMDDHHMM` (run start time,
**minute-granular**), sanitizing any non-`[A-Za-z0-9_-]` run to a single `_`. The leading `qc`
segment makes these workspaces easy to identify/group/delete (they accumulate — R4); the infix also
names the **export flow**: `qc_anvil_ade` (`ade-qc`; `ade` = AnVIL Data Explorer, the public name,
preferred over the backend service name `azul`) or `qc_anvil_tdr` (`tdr-qc`). The infix may evolve and
is a parameter of `workspace.workspace_name` (`ADE_NAME_INFIX` / `TDR_NAME_INFIX`). Minute granularity
(not per-day) is for the **batch** use case (the primary one): a QC workflow that fails transiently
can be **rerun without colliding** on an existing workspace — Terra workflows take minutes to start,
so minute resolution suffices. **Fail fast** if the name already exists (now a rare same-minute edge).
Trade-off: workspaces accumulate per run (see risk R4). Create with `authorizationDomain: []` and
`enhancedBucketLogging: true`; the import applies the auth domain for controlled-access datasets — we
**verify**, never set it.

**BigQuery is core, not deferred:** it resolves the Azul `dataset_id` from the snapshot's
`anvil_dataset` table at the *start* of the export, in addition to the later table comparison.

**Logging:** centralized, structured request/response logging in the HTTP client base — redacted
tokens/signatures, plus correlation/trace headers (`X-Cloud-Trace-Context`, `traceparent`,
`X-Request-ID`); a top-of-log header with tool version + per-service version/status + resolved
identity; the detailed log emitted as a **WDL output**; tier+snapshot+timestamp in the filename. No
automatic Google/Cloud service-log capture. Mirror the patterns in `drs-test-notebooks`
(`notebooks/common/common_functions.ipynb`).

**Comparison-engine seams (deferred build):** a common `TabularDataSource` interface (Rawls +
BigQuery readers), profile-driven expected-difference handling, and a `populate(strategy) →
compare(…)` pipeline split — so added flows (TDR→AW direct, ADSW ingestion QC) are additive.

**Process:** discuss design and get **explicit approval before scaffolding** structural code.

## Documentation is a first-class deliverable

This is a long-lived, multi-person project; **excellent documentation is vital to its longevity and
success** and is treated as part of the work, not an afterthought. Two audiences:

- **Users** (AnVIL data-release QA): what the tool checks per flow, how to run it (per-tier ADC
  login, config, CLI + inputs), and how to interpret the report. **Required:** a prominent
  *"Limitations & where human review matters most"* section that states what the tool does **not**
  verify, which differences it normalizes away (and so won't flag), known soft spots (DRS/file-
  reference forms, type-representation edge cases), and an explicit "still review X, Y, Z" list
  pointing to the retained workspace. Honest limitations are what make the automation trustworthy.
- **Maintainers** (esp. when service APIs change): architecture/flows, the service registry
  ([`services.md`](docs/services.md)), the HAR re-capture procedure
  ([`capturing_http_traffic.md`](docs/capturing_http_traffic.md)) for API drift, how to update
  clients/comparison-profiles, dev setup/tests/replay fixtures, build/deploy (Docker/WDL), and the
  [risk register](docs/risk_register.md).

Keep `docs/` updated as functionality lands — it already holds the maintainer-facing reference
material and should grow the user-facing usage/limitations docs as the flows are implemented.

## Working with the maintainer

- The maintainer has **deep domain knowledge** of these services and their APIs and will provide
  context, specifics, and Swagger files before planning/design/implementation. **BigQuery SQL** is
  the one area where they explicitly want Claude's help.
- This is intended to be **long-lived, multi-author** code — prioritize clarity, ease of use, and
  maintainability over cleverness.
