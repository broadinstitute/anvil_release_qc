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
| `tdr_qc.wdl` | `tdr-qc` | Same QC via a **direct TDR export** (no ADE/Azul) — for very large datasets that fail ADE export. **No Azul token needed** (Terra creds throughout). Open-access only for now. |
| `compare.wdl` | `compare` | Re-QC an **existing** Analysis Workspace against its TDR snapshot (no import). |
| `compare_ingest.wdl` | `compare-ingest` | Ingestion QC: submitter (ADSW) workspace vs TDR snapshot. |

Each has a matching `*.inputs.example.json`. Fill in `config`, the workspace/snapshot ids, and `docker`.

## 3. Runtime model (credentials & config)

- **ADC = the ambient Terra runtime service account** (no key file). The CLI identity guard resolves
  that pet SA to its **owning human Terra user** via Sam (`/api/users/v2/self`) and asserts it is one of
  the tier's authorized users — so **the deployment config's `email` is your human Terra account** (the
  same value you use locally), *not* the pet service account. For **multiple operators**, list their
  human accounts under the tier's `authorized_users:` (the guard and the Azul-token check allow any
  account in `email` ∪ `authorized_users`; each operator mints their own Azul token as themselves).
- **`config` is a File input** — the same `config.yaml` the CLI uses, with **`adc_credentials_file: null`**
  so ADC falls back to that runtime identity. Store it in a bucket and pass its `gs://` path.
- **`ade-qc` is open-access by default**: Azul is queried anonymously (the consent gate enforces
  `consentCode == "NRES"`), so **no Azul token is needed**. `compare` / `compare-ingest` never touch Azul.
- **`tdr-qc` never touches Azul** — it exports directly from TDR using the runtime Terra credentials, so
  it needs **no Azul token at all** (no `azul_token` input). It is **open-access only for now**:
  controlled-access `tdr-qc` is blocked pending confirmation of the import's permission-sync behavior
  (see [`docs/tdr_export_flow.md`](../docs/tdr_export_flow.md)); `allow_controlled_access` is accepted
  for parity but still blocks. Simpler inputs than `ade_qc.wdl` (no `catalog`, no `azul_token`).

### Controlled-access (`ade-qc` only)

Controlled-access is **security-team approved** (no restrictions — all AnVIL data) and gated behind an
explicit, default-off opt-in. To QC a controlled-access snapshot:

1. **Mint a fresh Azul token** locally: `anvil-data-qc azul-login --tier prod` (controlled data needs a
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
| `qc_exit_code` (Int) | `0` = PASS, `1` = QC failure, `2` = safety abort (e.g. a non-open-access snapshot blocked by the consent gate) |
| `qc_report` (File) | the human-readable comparison report |
| `detailed_log` (File) | the full redacted request/response log |

The task **always succeeds** so the report is always produced (a *failed* Cromwell task delocalizes no
outputs). Identify failures by the `qc_passed` / `qc_exit_code` columns — no need to open each report.

## 5. Validate WDL changes locally

Terra runs Cromwell; validate with the matching [womtool](https://github.com/broadinstitute/cromwell/releases)
before pushing (this repo was validated with womtool 92):

```bash
java -jar womtool-92.jar validate wdl/ade_qc.wdl
java -jar womtool-92.jar validate -i wdl/ade_qc.inputs.example.json wdl/ade_qc.wdl
java -jar womtool-92.jar validate wdl/tdr_qc.wdl
java -jar womtool-92.jar validate -i wdl/tdr_qc.inputs.example.json wdl/tdr_qc.wdl
```
