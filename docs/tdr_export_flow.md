# TDR Export → Import Flow (TDR direct → Terra)

The empirically-resolved API flow for exporting a dataset **directly from the TDR snapshot** (no
ADE/Azul) and importing it into a Terra Analysis Workspace, derived from **interactive traffic captured
2026-06-27** (see [`capturing_http_traffic.md`](capturing_http_traffic.md)). This is the reference spec
the `tdr-qc` automation mirrors. Endpoints and per-tier URLs: [`services.md`](services.md).

This is the **second** export/QC flow, alongside the ADE/Azul flow
([`export_import_flow.md`](export_import_flow.md)). It exists because some datasets are too large to
export through ADE/Azul under its current configuration; TDR's direct export handles them. **Both flows
share the same QC** — the import tail (createWorkspace → importJob → poll) and the comparison
(AW-vs-snapshot tables + auth-domain) are identical; only the *export head* differs.

> **Driving principle — the TDR snapshot is the system of record.** As with the ADE flow, the process
> is driven by the snapshot id and data read from the snapshot.

> **Validation status (2026-06-27):** run end-to-end against the **prod** tier for the **open-access
> 1000G** snapshot (`e8dacaea-…`): TDR export → manifest → `createWorkspace` → `importJob (tdrexport)`
> → `Done`. (The dev attempt failed with a generic "Failed to fetch" — an under-resourced-dev-tier
> flake, not a flow problem; prod succeeded first try.)

> **⚠️ Safety gate — open-access only (for now).** `tdr-qc` runs only on snapshots whose top-level
> `consentCode` is exactly `NRES`. Controlled-access `tdr-qc` is **blocked entirely** — even with
> `--allow-controlled-access` — until the `tdrSyncPermissions` question below is resolved (see
> `safety.assert_tdr_qc_permitted` and risk **R8**).

## Authentication

**One credential — the Terra/ADC token — serves the whole flow.** Unlike the ADE flow (which needs a
separate Azul token), every call here goes to a Terra-tier service that accepts the gcloud ADC token:
TDR (`data.terra.bio` / dev `jade.datarepo-dev…`), Rawls, Firecloud, Sam. The token is **refreshable**
(`google-auth` mints a fresh access token at the ~1 h boundary), so the long import survives. **No Azul
token, no `azul-login`.** The signed manifest/parquet URLs are produced and signed by TDR's own service
account; the Terra import service fetches them server-side.

## Flow

### 1. Start the snapshot export (TDR — asynchronous)
- **GET** `https://<tdr>/api/repository/v1/snapshots/<snapshotId>/export?exportGsPaths=false&validatePrimaryKeyUniqueness=true`
  → `202` with a TDR job: `{"id": "<jobId>", "job_status": "running", "status_code": 202, ...}`.
- **Params** (mirroring the UI; constants in `clients/tdr.py`):
  - `exportGsPaths=false` — return **signed https URLs** (what the import needs), not `gs://` paths.
  - `validatePrimaryKeyUniqueness=true` — validate PK uniqueness during export; a non-unique PK
    **fails the export** (a legitimate QC finding). Flip to `false` if it proves over-strict.

### 2. Poll the export job (TDR)
- **GET** `https://<tdr>/api/repository/v1/jobs/<jobId>` while `job_status == "running"` (HTTP 202),
  until `job_status == "succeeded"` (HTTP 200). `job_status == "failed"` ⇒ error. (1000G export ~40s;
  large datasets take longer — poll patiently with a generous `max_wait`.)

### 3. Read the result → the signed manifest (TDR)
- **GET** `https://<tdr>/api/repository/v1/jobs/<jobId>/result` →
  `{"snapshot": {...}, "validatedPrimaryKeys": true, "format": {"parquet": {...}}}`.
- The import URL is **`format.parquet.manifest`** — a signed GCS URL to the export's `manifest.json`:
  `https://storage.googleapis.com/<bq_project>-snapshot-export-bucket/<jobId>/manifest.json?X-Goog-…`
  (the bucket is the snapshot's **own BigQuery project** + `-snapshot-export-bucket`).
- `format.parquet.location.tables[].paths[]` lists per-table parquet signed URLs — **the automation
  does not use these** (the import reads them via the manifest). Wrap only the manifest in `SignedUrl`;
  **do not log the raw result** (it is full of signed URLs).

### 4. Create the workspace (Rawls) — **empty** auth domain
- **POST** `https://<rawls>/api/workspaces` — **identical body to the ADE flow**
  (`authorizationDomain: []`, `attributes.description`, `copyFilesWithPrefix: "notebooks/"`,
  `bucketLocation: "US-CENTRAL1"`, `enhancedBucketLogging: true`, `addUsers: []`) → `201`.

### 5. Import the export (Firecloud — asynchronous, long-running)
- **POST** `https://<firecloud>/api/workspaces/<ns>/<name>/importJob` — **same endpoint as the ADE
  flow**, with:
  ```json
  {"url": "<signed manifest.json URL>", "filetype": "tdrexport", "options": {"tdrSyncPermissions": false}}
  ```
  → `202` with `{"jobId": "<uuid>", ...}`. (ADE uses `filetype: "pfb"`, `options: null`.)
- **Poll** `GET …/importJob/<jobId>` → `{"filetype": "DATA_IMPORT", "jobId", "status"}`, same lifecycle
  as the ADE import (`Pending → Translating → ReadyForUpsert → Upserting → Done`, or `Error`). Require
  `Done` before QC.

### 6. QC (after import success) — **identical to the ADE flow**
- **Auth-domain** (`compare/authdomain.py`): Sam `…/datasnapshot/<id>/authDomain` (expected) vs Rawls
  workspace `authorizationDomain` (actual); empty/mismatch on a controlled dataset is a QC failure.
- **Tabular comparison** (`compare/`): AW data tables (Rawls entities) vs the TDR snapshot tables
  (BigQuery), T1–T2 (table parity, row counts, column-count sanity), with the usual normalizations.

## ⚠️ `tdrSyncPermissions` — open safety question (resolve before controlled-access)

The captured importJob used `options.tdrSyncPermissions = false`. The maintainer's understanding is that
TDR snapshot permissions **generally are synced (`true`)** from the snapshot to the workspace, and the
`false` here is likely **because 1000G is open-access**. Mirroring `false` onto **controlled** data
could leave the workspace **under-protected — a potential Federal DMI**. Therefore:

- `tdrSyncPermissions` is **not hardcoded blindly**; `tdr-qc` mirrors the open-access capture (`false`)
  and is **open-access-only**.
- The correct **controlled-access** value (likely `true`) — and whether `tdrexport` applies the
  snapshot's auth domain at all — **must be confirmed** (ideally via a fresh controlled-snapshot
  capture/test) **before** controlled-access `tdr-qc` is enabled. This is enforced in code:
  `safety.assert_tdr_qc_permitted` blocks any non-open-access snapshot.
- Backstop: even if permission sync were wrong, the **QC auth-domain check would catch it** for a
  controlled snapshot (Sam-expected non-empty vs workspace-actual empty ⇒ QC **FAIL**) — but that is a
  *post-hoc* detector; we want the cause right, not just the detector.

## Safety mapping (the four layers — see [`../src/anvil_data_qc/safety.py`](../src/anvil_data_qc/safety.py))

1. **Consent gate** — `tdr-qc` is open-access only (`assert_tdr_qc_permitted`), enforced **before** the
   export (before any signed URL exists).
2. **Secret containment** — `format.parquet.manifest` is wrapped in `SignedUrl`; revealed only at the
   importJob site. The raw export result (many signed URLs) is never logged.
3. **Hand-off verification** — destination pinned to the tier's Firecloud host; **provenance** =
   `https://storage.googleapis.com/<bq_project>-snapshot-export-bucket/` (derived from the snapshot's
   own BigQuery project — `safety.tdr_export_allowed_prefixes`). Tighter than the ADE fixed allow-list.
4. **Bearer-token host-pinning** — unchanged (a token only ever reaches its own HTTPS host).

## Notes

- **No Azul, no signed-S3 URL, no Azul token** — contrast with [`export_import_flow.md`](export_import_flow.md).
- All async steps (§1–2, §5) are **poll-until-terminal**; never assume immediate completion.
- BigQuery is still needed for the QC comparison (and to derive the export bucket for provenance), via
  the snapshot's `accessInformation.bigQuery`.
