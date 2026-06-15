# anvil-data-qc

**Automated quality control for AnVIL data releases.**

> **Status:** validated end-to-end against **production** for **open-access** datasets. Controlled-access
> support is gated pending a security review — see [Security model](#security-model).

## Table of contents

- [Overview](#overview)
- [Purpose](#purpose)
- [What it does](#what-it-does)
- [Who this is for — start here](#who-this-is-for--start-here)
- [Using it (quickstart)](#using-it-quickstart)
- [What it checks — and what it doesn't](#what-it-checks--and-what-it-doesnt)
- [Security model](#security-model)
- [Running in Terra (Docker + WDL)](#running-in-terra-docker--wdl)
- [Repository structure](#repository-structure)
- [For maintainers / development](#for-maintainers--development)
- [Documentation](#documentation)
- [Status & license](#status--license)

## Overview

AnVIL hosts hundreds of biomedical datasets, each combining relational tabular data with cloud file
blobs. Every dataset is released to researchers through a four-system pipeline:

```
ADSW  ──ingestion──▶  TDR  ──indexing──▶  ADE  ──PFB export──▶  AW
```

- **ADSW** — AnVIL Data Submittal Workspace: the Terra workspace where a submitter's tabular + file data lands.
- **TDR** — Terra Data Repository: the dataset is ingested here and exposed as a **snapshot** — the **source of truth** for QC.
- **ADE** — AnVIL Data Explorer: indexes the dataset and exports it as **PFB**.
- **AW** — Analysis Workspace: a researcher's Terra workspace that receives the export.

`anvil-data-qc` verifies each hop of this pipeline automatically, so a release can be trusted
end-to-end.

## Purpose

With hundreds of datasets, release verification done by hand doesn't scale — and the stakes are high:
data can be **silently lost** between systems, and **controlled-access metadata must stay protected**.
This tool replaces the manual checks with **thorough, API-driven automation** that:

- treats the **TDR snapshot as the source of truth** and compares each downstream copy against it,
- runs **fail-closed** — proceeding only when it can prove a dataset is open-access (see [Security model](#security-model)),
- produces a clear PASS/FAIL report on every run, suitable for batch use across many datasets.

## What it does

Three operations, one per concern:

| Command | Verifies | Pipeline hop | Typically run by |
|---|---|---|---|
| **`compare-ingest`** | every submitter table was ingested into TDR with the same row count | ADSW → TDR | Operations (ingestion) |
| **`qc`** | a snapshot exports/imports into a new Analysis Workspace, then that workspace matches the snapshot (tables, rows, columns, auth domain) | TDR → ADE → AW | Operations / data QA |
| **`compare`** | an existing Analysis Workspace still matches its snapshot (no re-import) | AW ↔ TDR | Data QA |

All runs are **open-access only** by default — a fail-closed safety gate blocks anything else (see
[Security model](#security-model)).

## Who this is for — start here

| If you are… | Start with |
|---|---|
| **Operations** (running ingestion / releases) | [Using it](#using-it-quickstart) · [Running in Terra](#running-in-terra-docker--wdl) |
| **Data curation / QA** | [What it checks — and what it doesn't](#what-it-checks--and-what-it-doesnt) |
| **Security** | [Security model](#security-model) |
| **Just want to try it** | [Using it (quickstart)](#using-it-quickstart) |

## Using it (quickstart)

### Prerequisites

- **Python 3.13** (the repo pins 3.13.x via `.python-version`).
- The **`gcloud` CLI** for credentials.
- Access to the AnVIL/Terra **tier** you'll run against (dev or prod), with a Google account authorized
  for TDR, BigQuery, and Terra.

### Setup

```bash
# 1. Clone, create a virtualenv, install
git clone https://github.com/mbaumann-broad/anvil-data-qc.git
cd anvil-data-qc
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure your per-tier settings (config/config.yaml is gitignored)
cp config/config.example.yaml config/config.yaml
#    fill in, per tier: email, adc_credentials_file, billing_project, azul_token_file

# 3. Sign in to Google (serves TDR, BigQuery, Terra). The active account must match
#    the tier's configured `email` — enforced at runtime by the identity guard.
gcloud auth application-default login
```

Open-access datasets need nothing more. (Controlled-access additionally requires
`anvil-data-qc azul-login --tier <tier>` — deferred pending the security review.)

### The common commands

**Ingestion QC — did the submitter's data make it into TDR?** (the most common day-to-day check)

```bash
anvil-data-qc compare-ingest \
  --submitter-workspace-namespace <billing-project> \
  --submitter-workspace-name <workspace-name> \
  --snapshot-id <tdr-snapshot-id> \
  --tier prod
```

**Full release QC — export a snapshot to a new workspace and verify it:**

```bash
anvil-data-qc qc --snapshot-id <tdr-snapshot-id> --tier prod --catalog anvil14
#   add --dry-run to validate the export side only (no workspace created, no import run)
```

**Re-check an existing Analysis Workspace against its snapshot:**

```bash
anvil-data-qc compare \
  --snapshot-id <tdr-snapshot-id> \
  --workspace-namespace <billing-project> --workspace-name <workspace-name> \
  --tier prod
```

Every command exits **`0` on PASS** and **non-zero on failure**, so it gates cleanly in scripts and
workflows.

### Reading the report

```
Ingestion comparison — PASS (5 submitter tables checked, 0 failing)
  reference (submitter): submitter workspace anvil-datastorage/1000G-high-coverage-2019
  candidate (TDR):       TDR snapshot BigQuery datarepo-13858a9f.ANVIL_1000G_high_coverage_2019_...
    OK  participant                rows     3202 -> 3202     OK
    OK  sample                     rows     3202 -> 3202     OK
    ...
  out of scope — 15 TDR tables with no submitter counterpart (FSS / system, not checked)
```

- the first line is the headline **PASS / FAIL**; each table is marked **`OK`** or **`XX`**,
- **`rows A -> B`** compares the reference (left) to the candidate (right),
- **"out of scope" / "excluded"** lines list tables intentionally not compared — see
  [What it checks](#what-it-checks--and-what-it-doesnt).

## What it checks — and what it doesn't

The comparison is organized into **four tiers of increasing depth**:

- **T1 — Table parity** *(now)* — every expected table is present (1:1).
- **T2 — Row & column counts** *(now)* — row counts match per table, and no column is dropped.
- **T3 — Row identity** *(deferred)* — the *same* rows are present (id-set parity).
- **T4 — Cell values** *(deferred)* — cell-by-cell value comparison.

The release flow additionally verifies the **authorization domain**. Expected differences are
normalized away so they don't cause false failures: empty source tables/columns Terra drops on import,
TDR system tables (`datarepo_row_ids`), and import-added tables (the DUOS registration record).

Because today's checks are **count-level (T1–T2)**, equal counts do not prove the *same* rows or values
are present — so for high-stakes datasets, **human review still matters**.

→ Full detail, the normalizations, and the **"Limitations & where human review matters most"** list:
**[`docs/comparison.md`](docs/comparison.md)**.

## Security model

A controlled-access PFB **signed URL** grants direct access to an NIH study's controlled data;
mishandling one is a Federal Data Management Incident (DMI). The tool is built **fail-closed** around
that risk:

1. **Consent gate** — the pipeline proceeds only when the snapshot's `consentCode` is exactly **`NRES`**
   (open-access); anything else, including empty or unknown, is blocked **before any signed URL is generated**.
2. **Signed-URL containment** — the PFB signed URL is a guarded value with a single reveal point (the
   import hand-off); it cannot be logged or printed.
3. **Same-tier hand-off verification** — the signed URL is only ever delivered to the expected,
   same-tier import service and storage bucket.
4. **Bearer-token host-pinning** — each service token is only ever sent to its own HTTPS host.

**Controlled-access is not yet enabled** — it requires a security-team review of the end-to-end flow
first. Open-access production datasets (e.g. the 13 currently released) are fully supported today.

→ See [`src/anvil_data_qc/safety.py`](src/anvil_data_qc/safety.py) and risk **R8** in
[`docs/risk_register.md`](docs/risk_register.md).

## Running in Terra (Docker + WDL)

The same code runs non-interactively in Terra as a **code-only Docker image** driven by three **WDL**
workflows (`qc`, `compare`, `compare-ingest`). Credentials come from the workspace's runtime service
account; the per-tier config is a workflow input; each run emits a **`qc_passed`** output you can
sort/filter to triage failures across many datasets at once.

→ Build → push → run, and the full runtime model: **[`wdl/README.md`](wdl/README.md)**.

## Repository structure

```
anvil-data-qc/
├── src/anvil_data_qc/        # the importable package (all logic)
│   ├── cli.py                #   CLI entrypoint: qc, compare, compare-ingest, azul-login
│   ├── config.py             #   YAML + pydantic config; tier resolution
│   ├── auth.py, azul_auth.py #   Google ADC + identity guard; Azul token capture
│   ├── safety.py             #   the fail-closed safety model (consent gate, signed-URL containment, …)
│   ├── pipeline.py           #   the wired export/import + QC flow
│   ├── clients/              #   HTTP clients: tdr, azul, rawls, firecloud, sam (+ redacting base)
│   └── compare/              #   comparison engine: sources, engine, report, qc, ingest, …
├── wdl/                      # Terra/WDL wrappers + deploy guide (README.md)
├── docs/                     # architecture, service registry, flows, comparison, risks
├── scripts/                  # git-secrets setup, HAR scrubbing
├── config/                   # config.example.yaml (copy to the gitignored config.yaml)
├── Dockerfile                # code-only image for batch / WDL runs
└── CLAUDE.md                 # architecture & design context (maintainers)
```

## For maintainers / development

```bash
pip install -e '.[dev]'                 # dev deps: pytest, ruff, pip-tools
pytest                                  # unit tests (mocked HTTP, no network)
ruff check . && ruff format --check .   # lint + format
```

- **Before committing**, install the secret-scanning hooks: `./scripts/setup-git-secrets.sh` (blocks
  tokens, keys, and signed-URL signatures). Requires [git-secrets](https://github.com/awslabs/git-secrets).
- **Capturing/refreshing API flows** (when a service API changes): follow
  [`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md), and always scrub a capture —
  `python scripts/scrub_har.py <capture>.har`. Raw captures live under `tmp/` (gitignored), never committed.
- **WDL changes**: validate with womtool (Terra's Cromwell) — see
  [`wdl/README.md`](wdl/README.md#5-validate-wdl-changes-locally).
- Architecture, conventions, and design rationale: [`CLAUDE.md`](CLAUDE.md).

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture, design decisions, conventions.
- [`docs/services.md`](docs/services.md) — service registry (repos, OpenAPI specs, per-tier URLs).
- [`docs/export_import_flow.md`](docs/export_import_flow.md) — the resolved ADE→Terra export/import API flow.
- [`docs/comparison.md`](docs/comparison.md) — what the comparisons check, and their limits.
- [`docs/risk_register.md`](docs/risk_register.md) — tracked project risks (incl. R8, the DMI risk).
- [`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md) — capturing & scrubbing HAR flows.
- [`wdl/README.md`](wdl/README.md) — Docker + WDL deployment.

## Status & license

Validated end-to-end against **production** for **open-access** datasets — the full
`ADSW → TDR → ADE → AW` pipeline is under automated QC. Controlled-access is deferred pending a security
review. Licensed under [`LICENSE`](LICENSE).
