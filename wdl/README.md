# Deploying anvil-data-qc (Docker + WDL)

The same code that runs locally and in notebooks ships as a Docker image and runs non-interactively in
Terra via the WDL workflows here. The image is **code-only** — no config, no secrets; everything
sensitive is supplied at run time.

## 1. Publish the image to Google Artifact Registry (GAR)

The image is published to GAR and referenced by the workflows below:

```
us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:<tag>
```

**One-time setup** (per project):

```bash
gcloud services enable artifactregistry.googleapis.com --project dsp-sa-development
gcloud artifacts repositories create anvil-qc \
  --repository-format=docker --location=us-central1 --project=dsp-sa-development \
  --description="anvil-data-qc images"
gcloud auth configure-docker us-central1-docker.pkg.dev   # let docker push to GAR
```

**Build and push** (tag defaults to the `pyproject.toml` version):

```bash
scripts/publish-image.sh             # or pass an explicit tag: scripts/publish-image.sh 0.1.1
```

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
| `qc.wdl` | `qc` | Full export/import + QC: export a TDR snapshot to a **new** Analysis Workspace and verify it. **Open-access only.** |
| `compare.wdl` | `compare` | Re-QC an **existing** Analysis Workspace against its TDR snapshot (no import). |
| `compare_ingest.wdl` | `compare-ingest` | Ingestion QC: submitter (ADSW) workspace vs TDR snapshot. |

Each has a matching `*.inputs.example.json`. Fill in `config`, the workspace/snapshot ids, and `docker`.

## 3. Runtime model (credentials & config)

- **ADC = the ambient Terra runtime service account** (no key file). The CLI identity guard asserts that
  account's email equals the configured tier `email`, so **the deployment config's `email` must be the
  workflow's runtime service account**.
- **`config` is a File input** — the same `config.yaml` the CLI uses, with **`adc_credentials_file: null`**
  so ADC falls back to that runtime identity. Store it in a bucket and pass its `gs://` path.
- **`qc` is open-access only**: Azul is queried anonymously (the consent gate enforces
  `consentCode == "NRES"`), so **no Azul token is needed**. Controlled-access support (and a token
  input) is deferred until after the security review. `compare` / `compare-ingest` never touch Azul.

## 4. Outputs & failure triage

Every workflow runs to completion and emits:

| Output | Meaning |
|---|---|
| `qc_passed` (Boolean) | the headline result — **sort/filter this column** in your data table to find failures |
| `qc_exit_code` (Int) | `0` = PASS, `1` = QC failure, `2` = safety abort (e.g. a non-open-access snapshot blocked by the consent gate) |
| `qc_report` (File) | the human-readable comparison report |
| `detailed_log` (File) | the full redacted request/response log |

The task **always succeeds** so the report is always produced (a *failed* Cromwell task delocalizes no
outputs). Identify failures by the `qc_passed` / `qc_exit_code` columns — no need to open each report.

## 5. Validate WDL changes locally

Terra runs Cromwell; validate with the matching [womtool](https://github.com/broadinstitute/cromwell/releases)
before pushing (this repo was validated with womtool 92):

```bash
java -jar womtool-92.jar validate wdl/qc.wdl
java -jar womtool-92.jar validate -i wdl/qc.inputs.example.json wdl/qc.wdl
```
