# Onboarding a new operator

This guide is for a teammate who will **run AnVIL data-release QC** — the `anvil-release-qc` pipelines
(`ade-qc` / `tdr-qc` / `compare` / `compare-ingest`) and/or the `qc-batch-runner`. For basic install and
the command quickstart, start with the [README](../README.md#using-it-quickstart); this doc focuses on
the **credentials, access, and verification** an operator needs to get set up cleanly.

## Read this first: access is Terra's, not the tool's

The tool keeps **no list of allowed users**. At startup it resolves your active credentials to your Terra
user (via Sam) and **logs** who is running; everything you can actually do is decided by your own Terra
permissions:
- access to the **shared QC workspace** (for the batch runner),
- the **`anvil-developer-work`** billing project (where QC workspaces are created) and BigQuery access in
  **`terra-ec38e08d`** (where BigQuery jobs run) — the prod defaults,
- and, for **controlled datasets**, the relevant NIH/dbGaP access to the data.

**So enabling an operator = granting those Terra permissions.** Nothing in this repo needs to change.

**Two different Terra workspaces are involved — don't confuse them:**
- **The shared QC workspace** (`qc_workspace_name`, e.g. `anvil-data-qc-shared`) — one long-lived
  workspace that holds the QC **data table** and that the **`qc-batch-runner` submits its workflows into**.
  Running the batch runner requires **write/compute access** to it (Writer-with-compute or Owner). The
  direct `ade-qc` / `tdr-qc` CLI does **not** use it at all — so a less-privileged account can still run
  the direct CLI (e.g. to test open-access data) without shared-workspace access.
- **The per-run Analysis Workspace** — **every** QC run (whether via the runner *or* the direct CLI)
  creates a **new, real, retained** Terra Analysis Workspace (named like
  `ops-validation-qc_anvil_<ade|tdr>_<snapshot>_<timestamp>`) that receives the exported data and is where you
  inspect the results. **These accumulate — one per run — and are yours to clean up periodically:** delete
  the ones you no longer need (the `ops-validation-` name prefix makes them easy to find and bulk-select). This is a
  known, expected operational cost — see [risk_register.md](risk_register.md) **R4**.

## Setup steps

1. **Terra access.** Get your Terra account granted the permissions listed above (billing project,
   BigQuery project, shared QC workspace if you'll use the batch runner, and controlled-data access for any
   controlled datasets you'll QC).

2. **Google credentials (ADC).** `gcloud auth application-default login` as your tier account. **Recommended:**
   save a **per-tier ADC file** and set `adc_credentials_file` in the optional local config, so the tool is *pinned* to that
   identity and **decoupled from your day-to-day `gcloud` account** (copy
   `~/.config/gcloud/application_default_credentials.json` to the per-tier path).
   - Credentials are loaded **once at startup and held in memory**, so you can **switch your ambient
     `gcloud`/ADC freely mid-run** — just don't `revoke` the account the run started with.
   - The saved ADC's refresh token is long-lived but subject to your org's Google **session/reauth
     policy**; it lapses eventually → re-run `gcloud auth application-default login` and re-save the
     per-tier file. (An expired credential produces a clear "re-login" message, not a stack trace.)

3. **Optional local config (`config/config.yaml`, gitignored).** Not required for prod — billing defaults to
   `anvil-developer-work` / `terra-ec38e08d`. Copy `config/config.example.yaml` if you want to save local
   settings: `adc_credentials_file`, the batch runner's `qc_workspace_name` (else pass
   `--qc-workspace-name`), or billing overrides (required for **dev**, which has no defaults — or pass
   `--billing-project` / `--billing-google-project-id`).

4. **Azul token (needed for `ade-qc` and any controlled-access data).** Run
   `anvil-release-qc azul-login --tier <t>`. It mints a token for **whichever Google account you sign in as**,
   so make sure that's your tier account. It **uploads** the token to
   `gs://fc-secure-886bea29-8c4e-4568-920a-a8cfee18e659/azul_token/<username>.txt` (`<username>` = your
   Terra email before the `@`; you need write access to that bucket). Every `ade-qc` run you start —
   locally, as a Terra workflow, or via the batch runner — reads it from there. The token is short-lived
   (~1 h) and is manually refreshed periodically during long runs. (Open-access-only runs need no token.)

## Verify your setup — `--verify-auth`

Before your first real run, confirm everything is wired up — **side-effect-free, no submissions:**

```bash
qc-batch-runner --tier <t> --table <table> --pipeline ade_qc --verify-auth   # batch runner
anvil-release-qc ade-qc --snapshot-id <id> --tier <t> --verify-auth             # a pipeline
```

It prints `✓ AUTH VERIFIED — operating as <you>; workspace … reachable; … Azul token …` on success, or
fails fast telling you exactly what's missing. This is the quickest way to shake out an incomplete setup.

## Troubleshooting — what the friendly errors mean

| Message | Cause | Fix |
|---|---|---|
| "Could not resolve the Terra user from Sam" | the credentials aren't a registered Terra user on this tier (e.g. dev credentials against prod) | sign in with your Terra account for that tier |
| "Tier 'dev' has no built-in default for …" | a dev run with no billing values | pass `--billing-project` / `--billing-google-project-id` (or set them in the local config) |
| "Could not authenticate … credentials … missing or expired" | ADC missing/expired | `gcloud auth application-default login` as the tier account, then re-save the per-tier file |
| "Access denied (HTTP 403) to Terra workspace …" | your account isn't shared on the workspace | ask the workspace owner to grant you access |
| `operating as` shows the **wrong** account | wrong `adc_credentials_file`, or logged in as the wrong account | point `adc_credentials_file` at the right account (or re-login) |
| Azul token rejected / wrong account | `azul-login` was run as the wrong Google account | re-run `azul-login` signed in as your tier account |
| `ade_qc` exits **2** with "Azul is (re)indexing…" (or an import "not found, or file was empty") | the ADE/Azul catalog is mid-(re)index → manifests are empty/incomplete | **wait** for the reindex to finish, then re-run — idempotency retries only the failures; check `GET …/health/progress`. See [qc_batch_runner.md → Azul reindexing](qc_batch_runner.md#azul-reindexing) |

## See also

- [README — quickstart](../README.md#using-it-quickstart) — install, config, the common commands.
- [qc_batch_runner.md](qc_batch_runner.md) — the batch runner (auth pacing, running under load).
- [services.md](services.md) — per-tier service endpoints.
- [risk_register.md](risk_register.md) — **R7** (Azul token), **R8** (controlled-access / DMI).
