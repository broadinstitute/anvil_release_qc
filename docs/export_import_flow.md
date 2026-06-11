# AnVIL Export → Import Flow (ADE/Azul → Terra)

The empirically-resolved API flow for exporting a dataset from the AnVIL Data Explorer (Azul) and
importing it into a Terra Analysis Workspace, derived from **interactive traffic captured 2026-06-10**
(see [`capturing_http_traffic.md`](capturing_http_traffic.md)). This is the reference spec the
automation mirrors. Endpoints and per-tier URLs: [`services.md`](services.md).

> **Driving principle — the TDR snapshot is the system of record.** The process is driven by the
> snapshot id and data read *from the snapshot*. Azul's identifiers are used only as **cross-checks**,
> never as inputs. If the downstream index (Azul) disagrees with the snapshot, that is a *finding*,
> not something to work around.

> **Validation status (2026-06-11):** the full flow has been run end-to-end against the **dev** tier
> for an **open-access** snapshot — TDR → BigQuery → Azul (anonymous) → `createWorkspace` →
> `importJob` → `Done`. The **controlled-access** path depends on resolving Azul auth (see
> [Authentication](#authentication)).

## Identifiers (worked example from the capture)

| Identifier | Example value | Source of truth |
|---|---|---|
| **TDR snapshot id** (the input) | `c6de2268-5d7a-4b0d-a055-8e558dbc60cc` | given to the tool |
| Snapshot name | `ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS_20240113_ANV5_202409261935` | TDR |
| BigQuery project | `datarepo-183d3f73` | TDR `retrieveSnapshot` |
| **Azul `dataset_id`** (manifest filter) | `265ecd43-c88f-19e9-4da1-b6cd1d07a557` | snapshot `anvil_dataset` table |
| Auth domain group | `AUTH_ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS` | snapshot (applied via Sam) |

Azul exposes the snapshot id as `sources[].source_id` and the dataset id as `datasets[].dataset_id`,
but we obtain `dataset_id` from the snapshot, not from Azul (see §1 and the secondary checks below).

## Open-access vs controlled-access datasets

AnVIL/TDR hosts both open-access and controlled-access data. The vast majority of the datasets are
controlled-access, and the open-access datasets tend to be very popular with researchers.

Ways to identify whether a dataset/snapshot is open- or controlled-access:
- The snapshot has two fields, **`phs_id`** and **`consent_code`**, both typically set to the valid
  dbGaP values. For **open-access** datasets, the snapshot `phs_id` is empty and the `consent_code`
  is set to **`NRES`** (meaning no restrictions).
- Additionally, **controlled-access** snapshots have an **Authorization Domain** set; **open-access**
  snapshots do not.

## Authentication

Two **distinct** credentials are needed — a single token does *not* satisfy every service:

- **TDR, BigQuery, and Terra (Rawls, Firecloud)** accept the **Google ADC token** (a plain
  `gcloud auth application-default` user token). Validated against dev.
- **Azul does NOT accept the gcloud ADC token.** Its registered-user validation
  (`TerraClient.for_registered_user(...).validate()`, in `src/azul/terra.py` + `oauth2.py`) requires a
  Google access token whose **`azp` (authorized party) equals Azul's own OAuth client id** (dev:
  `561542988117-…apps.googleusercontent.com`). Consequences:
  - **Open-access** datasets need **no token** — Azul serves them via its public service account, so
    the tool sends *no* `Authorization` header to Azul when no APAT is configured. ✅ validated.
  - **Controlled-access** datasets need an Azul-accepted token. The gcloud ADC token (`azp` = gcloud)
    is rejected (401), and an **APAT** from Azul's `GET /user/token` is *also* rejected by the
    registered-user validation (401 "Invalid credentials"; the APAT had `jti:0`). **Open question with
    the Azul team:** how should a *headless/batch* client obtain an Azul-accepted user token? (The
    interactive flow uses an authorization-code token with the right `azp`, issued BFF-style with no
    refresh token — awkward for batch, which is why the long-lived APAT looked promising.)

The tool models this with a per-tier optional **APAT** (`azul_apat_file`): when set, it is sent to
Azul; when unset/empty, Azul is called anonymously (open-access). The gcloud ADC token continues to
serve TDR/BigQuery/Terra.

## Flow

### 1. Resolve the Azul `dataset_id` from the snapshot (system of record)
- **TDR** `retrieveSnapshot(snapshotId)` → BigQuery access info (`accessInformation.bigQuery`:
  project + dataset name), snapshot name, etc.
- **BigQuery** — every AnVIL snapshot has an `anvil_dataset` table with a `dataset_id` column and
  **exactly one row**:
  ```sql
  SELECT dataset_id FROM `<bq_project>.<bq_dataset>.anvil_dataset`
  ```
  That `dataset_id` is the value Azul's manifest filter needs. **0 or >1 rows ⇒ error / finding.**

### 2. Generate the PFB manifest (Azul — asynchronous)
- **PUT** `https://<azul>/fetch/manifest/files?catalog=<catalog>&filters=<F>&format=verbatim.pfb`
  where `<F>` is the URL-encoded JSON `{"datasets.dataset_id":{"is":["<dataset_id>"]}}`.
  → `200 {"Status": 301, "Location": "https://<azul>/fetch/manifest/files/<token>", "Retry-After": <sec>}`
- **Poll** `GET https://<azul>/fetch/manifest/files/<token>` while `Status == 301` (honoring
  `Retry-After`), until `Status == 302`. The 302 `Location` is a **signed S3 URL** to the `.avro`
  PFB manifest. (The 302 body also includes a `CommandLine` with ready-made `curl` commands.)
- `format=verbatim.pfb` — "verbatim" mirrors the source, which is what QC wants.
- **Catalog (the data release):** a catalog is the indexed data for one AnVIL release. **Dev**
  defaults to catalog `anvil` (omit the param). **Prod** requires the *new release's* catalog
  explicitly (e.g. `catalog=anvil14`) — that release is being QC'd before it becomes the default, so
  do not rely on the prod default. The tool exposes `--catalog` (omit in dev; set in prod).
- **Auth:** see [Authentication](#authentication). Open-access works anonymously; controlled-access
  needs an Azul-accepted token.

### 3. Hand off to Terra
The interactive UI opens Terra at `https://<terra>/#import-data?format=PFB&url=<signed S3 PFB URL>`
(`anvil.terra.bio` and `app.terra.bio` are the same system). The automation skips the page and goes
straight to the API calls below using the signed URL from §2.

### 4. Create the workspace (Rawls) — **empty** auth domain
- **POST** `https://<rawls>/api/workspaces`
  ```json
  {"namespace": "<billing-project>", "name": "<workspace-name>", "authorizationDomain": [],
   "attributes": {"description": "<...>"}, "copyFilesWithPrefix": "notebooks/",
   "bucketLocation": "US-CENTRAL1", "enhancedBucketLogging": true, "addUsers": []}
  ```
  → `201`. **`authorizationDomain` is intentionally `[]`** — see §6.

### 5. Import the PFB (Firecloud Orchestration — asynchronous, long-running)
- **POST** `https://<firecloud>/api/workspaces/<namespace>/<name>/importJob`
  ```json
  {"url": "<signed S3 PFB URL>", "filetype": "pfb", "options": null}
  ```
  → `202` with `{"jobId": "<uuid>", "url": "<signed url>"}`. (The submit can be slow on dev — allow a
  generous client timeout.)
- **Poll** `GET https://<firecloud>/api/workspaces/<namespace>/<name>/importJob/<jobId>` →
  `{"filetype": "DATA_IMPORT", "jobId": "<uuid>", "status": "<status>"}`. Status lifecycle:
  `Pending → Translating → ReadyForUpsert → Upserting → Done` (success), or `Error` (failure).
  **Imports can run ~1 hour or more**, so poll patiently (the tool uses a generous `max_wait`).
  Require `Done` before any QC. *(There is also `GET …/importJob?running_only=true` to list jobs.)*

### 6. Authorization domain — applied automatically, then verified
The workspace is created with `authorizationDomain: []`. **The import service then sets the auth
domain as a privileged operation *after* creation:** it inspects the PFB, reads its
`source_datarepo_snapshot_id`, looks up the snapshot's auth domain(s), and applies them to the
workspace (bypassing the normal post-creation immutability). Confirmed in the capture — createWorkspace
sent `[]`; a later workspace `GET` returned
`authorizationDomain: [{"membersGroupName": "AUTH_ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS"}]`.

**Automation implication:** mirror the UI — create with `authorizationDomain: []` and let the import
apply it. **Do not fetch or set the auth domain ourselves.**

## QC verifications (after import reports success)

1. **Auth-domain protection** *(critical / compliance — risk R2)*: the tabular metadata is
   controlled-access and must be protected. Assert:
   - **expected** ← Sam `GET /api/resources/v2/datasnapshot/<snapshotId>/authDomain`
   - **actual** ← Rawls `GET /api/workspaces/<ns>/<name>` → `workspace.authorizationDomain[].membersGroupName`
   - **expected == actual.** An empty or mismatched workspace auth domain on a controlled dataset is
     a **QC failure**.
2. **Tabular comparison**: AW data tables (Rawls entities) vs the TDR snapshot tables (BigQuery) —
   same tables, columns, row counts, etc., with expected-difference normalization (design deferred).
3. **Secondary Azul-agreement checks** *(not on the critical path — findings, not drivers)*:
   - Azul `sources[].source_id` == the input snapshot id;
   - Azul `datasets[].dataset_id` == the snapshot's `anvil_dataset.dataset_id`.

## Notes

- **Auth:** see [Authentication](#authentication) — TDR/BigQuery/Terra use the gcloud ADC token; Azul
  needs its own (open-access = anonymous; controlled-access = unresolved). `Authorization` is always
  logged redacted; Terra calls also send an `x-app-id` header.
- **BigQuery is a core dependency**, needed in §1 (dataset_id resolution) — not only the comparison
  phase.
- The PFB manifest is an **Avro** file served from **S3** via an AWS presigned URL (temporary STS
  credentials) — hence the AWS-credential redaction in `scripts/scrub_har.py`.
- All async steps (§2, §5) are **poll-until-terminal**; never assume immediate completion.
