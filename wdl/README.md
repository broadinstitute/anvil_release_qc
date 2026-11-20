# Deploying anvil-release-qc (Docker + WDL)

The same code that runs locally and in notebooks ships as a Docker image and runs non-interactively in
Terra via the WDL workflows here. The image is **code-only** — no config, no secrets; everything
sensitive is supplied at run time.

## 1. Publish the image to Google Artifact Registry (GAR)

The image is published to GAR and referenced by the workflows below:

```
us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-release-qc:<tag>
```

**One-time setup** (per project):

```bash
gcloud services enable artifactregistry.googleapis.com --project dsp-sa-development
gcloud artifacts repositories create anvil-qc \
  --repository-format=docker --location=us-central1 --project=dsp-sa-development \
  --description="anvil-release-qc images"
gcloud auth configure-docker us-central1-docker.pkg.dev   # let docker push to GAR
```

**Build and push** (tag defaults to the `pyproject.toml` version):

```bash
scripts/publish-image.sh             # or pass an explicit tag: scripts/publish-image.sh 0.1.1
```

The script builds for **`linux/amd64`** (Terra/Cromwell's platform), so publishing from an Apple
Silicon (arm64) Mac still produces a Terra-compatible image.

**Grant Terra read access (private repo).** Terra runs each workflow as a per-user *pet service
account*; for Cromwell to pull a private image, that identity needs `reader` on the repo. Replace
`<TERRA_IDENTITY>` with your pet service account or proxy group (see Terra's "import a Docker image
from a private GCR/GAR" docs):

```bash
gcloud artifacts repositories add-iam-policy-binding anvil-qc \
  --location=us-central1 --project=dsp-sa-development \
  --member="<TERRA_IDENTITY>" --role="roles/artifactregistry.reader"
```

This published reference is the **default `docker` input** for every workflow, so you only override it
to run a different tag.

## 2. The workflows

| WDL | CLI command | Purpose |
|---|---|---|
| `ade_qc.wdl` | `ade-qc` | Full export/import + QC via the **ADE/Azul** export: export a TDR snapshot to a **new** Analysis Workspace and verify it. Open-access by default; controlled-access via the opt-in. |
| `tdr_qc.wdl` | `tdr-qc` | Same QC via a **direct TDR export** (no ADE/Azul) — for very large datasets that fail ADE export. **No Azul token needed** (Terra creds throughout). Open-access by default; controlled-access via the opt-in. |
| `compare.wdl` | `compare` | Re-QC an **existing** Analysis Workspace against its TDR snapshot (no import). |
| `compare_ingest.wdl` | `compare-ingest` | Ingestion QC: submitter (ADSW) workspace vs TDR snapshot. |

Each workflow has **two** committed config sets (see [§3](#3-runtime-model-credentials--config)):
- **`*.inputs.example.json`** — a safe, standalone illustration: literal placeholders, `allow_controlled_access: false`, womtool-validatable. Start here to learn the input shape.
- **`*.inputs.json` + `*.outputs.json`** — the **actual production** Terra method-config (the configs in use in `anvil-developer-work/anvil-data-qc-shared`), abstracted via Workspace Data + data-table references so they carry **no secrets or bucket paths** and are portable to any workspace that defines the required Workspace Data keys and data-table columns.

## 3. Runtime model (credentials & config)

- **ADC = the ambient Terra runtime service account** (no key file). The CLI identity guard resolves
  that pet SA to its **owning human Terra user** via Sam (`/api/users/v2/self`) and asserts it is one of
  the tier's authorized users — so **the deployment config's `email` is your human Terra account** (the
  same value you use locally), *not* the pet service account. For **multiple operators**, list their
  human accounts under the tier's `authorized_users:` (the guard and the Azul-token check allow any
  account in `email` ∪ `authorized_users`; each operator mints their own Azul token as themselves).
- **`config` is a File input** — the same `config.yaml` the CLI uses, with **`adc_credentials_file: null`**
  so ADC falls back to that runtime identity. Store it in a bucket and pass its `gs://` path. It must set
  **two distinct projects** per tier: **`terra_billing_project`** (the Terra billing project = workspace
  namespace that ade-qc/tdr-qc create workspaces under) and **`billing_google_project_id`** (a real Google
  Cloud Project ID — the Terra **Workspace** Google Project ID, e.g. `terra-xxxxxxxx` — that BigQuery jobs
  run in / are billed to; **not** the Terra billing project). `--verify-auth` preflights BigQuery access to it.
- **`ade-qc` is open-access by default**: Azul is queried anonymously (the consent gate enforces
  `consentCode == "NRES"`), so **no Azul token is needed**. `compare` / `compare-ingest` never touch Azul.
- **`tdr-qc` never touches Azul** — it exports directly from TDR using the runtime Terra credentials, so
  it needs **no Azul token at all** (no `azul_token` input), for open- *and* controlled-access. Controlled
  data uses the same default-off `allow_controlled_access` opt-in as `ade_qc.wdl` (the runtime identity's
  own Terra access must cover the controlled data — TDR `/export` 403s otherwise; the import applies the
  snapshot's auth domain, verified by the QC check — see [`docs/tdr_export_flow.md`](../docs/tdr_export_flow.md)).
  Simpler inputs than `ade_qc.wdl` (no `catalog`, no `azul_token`).

### Workspace Data setup (for the production `*.inputs.json`)

The production configs keep environment-specific and secret-adjacent paths **out of git** by binding
them to Terra attributes instead of literals — workspace-level values via `workspace.<key>` (the
analogue of a data-table row's `this.<column>`). Set these once per workspace under **Workspace Data**:

| Workspace Data key | Value | Used by |
|---|---|---|
| `anvil_data_qc_config` | `gs://` path to the `config.yaml` (with `adc_credentials_file: null`) | all four workflows (`config`) |
| `anvil_data_qc_azul_token` | `gs://` path to the Azul token file (a **secure** bucket) | `ade-qc` controlled-access only (`azul_token`) |

The `snapshot_id` and — for `ade-qc` — the `ade_catalog` come from the per-release **data table**; the
`config`/`azul_token` paths come from **Workspace Data**; `tier` and `docker` are non-sensitive literals.
So the committed `*.inputs.json` contain no bucket paths or tokens, and the same file runs across releases
and in any workspace that defines the Workspace Data keys above and the data-table columns (see §4).

These files are Terra **method-config** exports, so values use Terra's serialization: attribute
references and Boolean/numeric **expressions** appear as `"${…}"` strings — e.g.
`"${workspace.anvil_data_qc_config}"`, `"${this.snapshot_id}"`, `"${this.ade_catalog}"`, `"${true}"` —
while plain **String** literals (`tier`, `docker`) stay unwrapped. (An empty `"${}"` is just an optional
input left blank in the UI — strip those.) The `workspace.`/`this.` references resolve only inside Terra;
they are **not** standalone Cromwell inputs, so use the `*.inputs.example.json` (literal values) for
womtool — see [§5](#5-validate-wdl-changes-locally).

**Production vs. example — `allow_controlled_access`.** The example files keep `allow_controlled_access:
false` (the safe default, so "off unless you mean it" is what a reader sees first); the production
`ade_qc.inputs.json`/`tdr_qc.inputs.json` honestly show **`true`**. This is deliberate and **security-team
approved** (all AnVIL data, 2026-06). Committing `true` does **not** weaken the safety model: the
code/WDL default stays `false`, and the flag is **not itself an access grant** — access still requires the
operator's own Terra credentials to cover the data (TDR/Azul `403` otherwise), plus a valid Azul token for
`ade-qc`, and safety layers 2–4 apply on every run. Worst case is benign (a controlled config pointed at
open-access data just permits it + logs the audit `WARNING`; pointed at data you can't access, it 403s).
Versioning the `true` makes the controlled-access posture **auditable** rather than a hand-set, unversioned
Terra value. Unlike the config/token paths, this flag is kept an **explicit literal** (not a `workspace.`
attribute) precisely because it's a safety setting you want readable in the versioned config.

### Controlled-access (`ade-qc` only)

Controlled-access is **security-team approved** (no restrictions — all AnVIL data) and gated behind an
explicit, default-off opt-in. To QC a controlled-access snapshot:

1. **Mint a fresh Azul token** locally: `anvil-release-qc azul-login --tier prod` (controlled data needs a
   token; it lasts ~1 h).
2. **Upload the token file to a secure bucket**, e.g. `gs://fc-secure-…/azul-token-prod.txt`.
3. **Set two `ade_qc.wdl` inputs:** `allow_controlled_access = true` and `azul_token` = that `gs://` token
   file. The run then permits any `consentCode` and logs a controlled-access audit `WARNING`.

**Batch + token timing:** the token is used only at each run's *early manifest step* (the long import
runs on the auto-refreshing ADC), and each run pre-checks ≥30 min remaining. So launch the data-table
rows **together, soon after minting the token** (e.g. ~5 at a time) and one fresh token covers the
batch. Always pass the token as a **File** (never a String input) to keep it out of submission metadata.

## 4. Outputs & failure triage

Every workflow runs to completion and emits:

| Output | Meaning |
|---|---|
| `qc_passed` (Boolean) | the headline result — **sort/filter this column** in your data table to find failures |
| `qc_exit_code` (Int) *(`compare` / `compare-ingest` only)* | `0` = PASS, `1` = QC failure, `2` = safety abort (e.g. a non-open-access snapshot blocked by the consent gate) **or a preflight/config failure** (e.g. a misconfigured `billing_google_project_id`) |
| `qc_report` (File) | the human-readable comparison report (ends with the stage-timing summary line) |
| `detailed_log` (File) | the full redacted request/response log |

**Run timing and data volume** are no longer WDL outputs (so they are not written to the data table). The
`ade-qc` / `tdr-qc` task still runs the same steps and surfaces them in the files it does emit: the
stage-timing summary line and the snapshot data-volume line (`total_table_count=… | total_row_count=… |
total_value_count=…`) are appended to `qc_report`, and the run times are in `detailed_log`. See
[`docs/comparison.md`](../docs/comparison.md).

**`compare` / `compare-ingest` only** — run start/end wall-clock, captured by the WDL task around the CLI call
(so present on **every** row, including failures), as sortable data-table columns:

| Output | Meaning |
|---|---|
| `run_began` (String) | when the run started |
| `run_ended` (String) | when the run finished (success *or* failure) |

Both are **UTC, ISO 8601 to the second** (e.g. `2026-08-23T13:09:24Z`), so they sort chronologically and are
timezone-unambiguous.

**`ade-qc` / `tdr-qc` only** — the created **Analysis Workspace** the flow exports/imports into (empty if
no workspace was created, e.g. a dry run or a pre-import failure; **present even on a failed import**).
**The workspace is deleted automatically when the QC reports no validation errors** (so on a passing row
these columns name a workspace that no longer exists); on a QC failure or a failed import it is retained as-is
for investigation. Set the **`keep_workspace`** input (CLI `--keep-workspace`, default `false`) to keep it even after a pass. As data-table columns:

| Output | Meaning |
|---|---|
| `workspace_namespace` (String) | the workspace's Terra billing project (= `terra_billing_project`) |
| `workspace_name` (String) | the workspace name (`<user>_<flow>_<snapshot>_<YYYYMMDDHHMM>`) |

Having these in the table lets you **run `compare` against a workspace whose import outran the wait**
(or as `compare` gains finer-grained tiers), **discover another operator's run** (the workspace is
owner-only by default — see who created it, then request access for manual/automated follow-up), and
**bulk-clean-up** the retained (failed-run) workspaces (see R4).

The task **always succeeds** so the report is always produced (a *failed* Cromwell task delocalizes no
outputs). Identify failures by the `qc_passed` column (and `qc_exit_code` for `compare` / `compare-ingest`) — no need to open each report.
(QC comparison time is intentionally *not* reported — it's quick and not a user-experienced delay.)

### One data table per release, shared by `ade-qc` and `tdr-qc`

The shared QC workspace (`anvil-developer-work/anvil-data-qc-shared`) holds **one data table per AnVIL
release** — e.g. `anvil15_qc` for anvil15 — so multiple releases' QC results live side by side. Each row
is one snapshot; its **input columns** are `snapshot_id`, a human-readable snapshot name, and
**`ade_catalog`** (the Azul catalog for that release, e.g. `anvil15`). `ade-qc` reads the catalog from the
row (`this.ade_catalog`) rather than a hardcoded literal, so **one workflow config serves every release**.

The production `*.outputs.json` route **both** `ade-qc` and `tdr-qc` back to that same per-release table,
**prefixing** each flow's outputs so they coexist without collision:

- `ade-qc` outputs → **`ade_*`** columns (`ade_qc_passed`, `ade_qc_exit_code`, `ade_qc_report`,
  `ade_detailed_log`, `ade_run_began`, `ade_run_ended`, `ade_workspace_namespace`, `ade_workspace_name`,
  `ade_manifest_minutes`, `ade_workspace_create_minutes`, `ade_import_minutes`).
- `tdr-qc` outputs → **`tdr_*`** columns (`tdr_qc_passed`, …, `tdr_run_began`, `tdr_run_ended`,
  `tdr_workspace_namespace`, `tdr_workspace_name`, `tdr_export_minutes`, `tdr_import_minutes`).
- the three data-volume counts (`total_table_count` / `total_row_count` / `total_value_count`) are left
  **unprefixed and shared**: both flows compute them from the *same* TDR snapshot, so they coincide — and
  the shared column doubles as a **cross-flow consistency check** (a divergence would itself be a signal).

Why one table per release:
- the **inputs are shared** (`snapshot_id` + snapshot name + `ade_catalog`);
- it gives an **at-a-glance overview** of which datasets ran under which pipeline, and their results;
- it supports **qualitative (PASS/FAIL) and quantitative (durations) comparison** between the two export
  routes, and the table **exports to TSV** for spreadsheet graphing/reporting.

`compare` / `compare-ingest` use **unprefixed** columns (their own runs, typically a separate table) —
including `run_began` / `run_ended`; `compare-ingest` emits no count columns (an intentional partial
comparison).

## 5. Validate WDL changes locally

Terra runs Cromwell; validate with the matching [womtool](https://github.com/broadinstitute/cromwell/releases)
before pushing (this repo was validated with womtool 92):

```bash
java -jar womtool-92.jar validate wdl/ade_qc.wdl
java -jar womtool-92.jar validate -i wdl/ade_qc.inputs.example.json wdl/ade_qc.wdl
java -jar womtool-92.jar validate wdl/tdr_qc.wdl
java -jar womtool-92.jar validate -i wdl/tdr_qc.inputs.example.json wdl/tdr_qc.wdl
```

Validate `-i` against the **`*.inputs.example.json`** (literal values). The production `*.inputs.json`
use Terra `workspace.`/`this.` references that resolve only inside Terra, so they are not standalone
Cromwell inputs — Terra validates those. A repo test (`tests/test_wdl_versions.py`) separately pins every
committed `anvil-release-qc:<tag>` image reference (WDL defaults + all inputs JSON) to `__version__`.
