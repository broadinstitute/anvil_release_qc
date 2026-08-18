# qc-batch-runner — batch execution of a QC pipeline over many snapshots

`qc-batch-runner` is a **meta-pipeline**: a separate CLI, in this repo, that runs one of the existing QC
pipelines (`ade-qc` or `tdr-qc`) over a **list of TDR snapshots**, holding at most a few workflows in
flight at once. It exists because a release can carry **hundreds of datasets** (the anvil15 release has
464), and driving them by hand — launching waves of ~5, refreshing the Azul token, triaging results —
is slow and error-prone.

It **orchestrates** the Terra workflows; it does not re-implement the QC. Each dataset's verdict still
lands in the shared workspace's data-table columns (`<prefix>_qc_passed` / `<prefix>_qc_exit_code`,
where `<prefix>` is `ade` or `tdr`) exactly as a single interactive run would; the runner reads those to
classify outcomes and to skip datasets that already passed.

> **Layer, not a fifth QC command.** The four QC operations (`ade-qc`, `tdr-qc`, `compare`,
> `compare-ingest`) live in the `anvil-data-qc` CLI. `qc-batch-runner` is a deliberately separate CLI a
> layer above them — it submits the pipelines' **Terra method configs** via the submissions API rather
> than calling the pipeline code. See [`README.md`](../README.md) and [`CLAUDE.md`](../CLAUDE.md).

## How the pieces fit

```
snapshot list (TSV)  ──▶  qc-batch-runner  ──POST submissions──▶  Terra (Rawls/Firecloud)
                                │                                        │
                                │  poll + read rows                      ▼
                                └──────────────────────────  shared QC workspace data table
                                                             (ade_qc_* / tdr_qc_* columns)
```

The runner submits the pipeline's **method config** once per snapshot (single-entity submissions),
polls each submission to a terminal state, then reads the resulting row to decide the outcome.

## Inputs

**From config (per tier — reuses [`config.py`](../src/anvil_data_qc/config.py)):** `tier`, credentials
(ADC + the identity guard), the workspace namespace (= `terra_billing_project`), and a new per-tier
**`qc_workspace_name`** — the existing shared QC workspace the runner submits into (also overridable on
the CLI). Everything else (service URLs, authorized users, Azul OAuth client id) comes from the same
config the QC commands use.

**From the CLI:**

| Option | Meaning |
|---|---|
| `-p, --pipeline` | `ade_qc` or `tdr_qc` (v1). |
| `--table` | Root entity type = the data-table name the method config is bound to (e.g. `anvil15_qc`). |
| `-s, --snapshots` | A **TSV with a `snapshot_id` column** — the rows to process (order-preserving, de-duplicated). Other columns are ignored, so you can export a data-table selection directly. |
| `-t, --tier` | `dev` \| `prod` (defaults to the config's `default_tier`). |
| `--qc-workspace-name` | Override the config's `qc_workspace_name`. |
| `--max-concurrent` | Max workflows in flight (default **5**, hard ceiling **10**). |
| `--dry-run` | Validate + print the effective plan; create **no** submissions. |
| `--force` / `--rerun-passed` | Re-run rows that already passed (default: skip them). |
| `--run-skipped` | Also run rows marked to skip via the `qc_skip` column (default: exclude them). |
| `--on-stale-token` | ade_qc Azul-token handling when TTL is low: `auto` (TTY→prompt, else wait) \| `prompt` \| `wait`. |
| `-c, --config` | Path to the YAML config (default `config/config.yaml`). |

## What it does

### 1. Pre-flight (fail-closed, before any launch)

Cheap, reuse-only checks; if any fails the batch stops **before** a single workflow is submitted:

- **Identity guard** on the operator's ADC (`auth.assert_tier_identity`) — the same authorized-user check
  the pipelines apply, resolved through Sam.
- **Method-config resolution** — the pipeline's config (`anvil-data-qc-ade-qc` / `anvil-data-qc-tdr-qc`)
  exists in the workspace and its **`rootEntityType` matches `--table`** (else the submissions would bind
  to the wrong table).
- **Row existence** — every `snapshot_id` in the TSV is an actual row in the table; any missing id is
  fatal (a deleted/replaced snapshot is a mistake worth stopping for).
- **Skip-marker cull (`qc_skip`)** — rows with a non-empty `qc_skip` column are **excluded** (not
  submitted) unless `--run-skipped`, each logged with its reason (see below). Checked *before* the
  idempotency cull and independently of `--force`.
- **Idempotency cull** — rows already `<prefix>_qc_passed == true` (or `<prefix>_qc_exit_code == 0`) are
  logged and **skipped** unless `--force`; an up-front "N excluded / N skipped / M run" summary is emitted.
- For `ade_qc`, a **token dry-check** (see below) is logged but not enforced in `--dry-run`.

It deliberately does **not** re-run the pipeline's full `--verify-auth` — from the runner's local
identity that isn't representative of the pet-SA TDR/BigQuery/export path the workflow actually uses.
That path is validated instead by the **cap-1 canary** (below).

### 2. Control loop (concurrency-capped)

A single loop polls the in-flight set, consults a **`can_launch()` policy**, and launches or waits until
everything is done:

- **v1 = batch-drain** (default): launch a wave of ≤`max_concurrent`, wait for it to fully drain, then
  the next wave.
- **v2 (future) = sliding window**: top up whenever in-flight < cap. Only the predicate changes — the
  loop, accounting, token-pacing, and classification are shared, because every launch is a
  **single-entity submission**.

Accounting is **fail-safe**: a submission counts as in-flight until it reports terminal (`Done` /
`Aborted`), and a **poll error keeps it in-flight** (unknown ⇒ occupied), so a transient API blip never
causes the runner to over-launch past the cap.

Terra imposes no user-facing small concurrency limit — **splitting the work into capped submissions is
the throttle**, to keep the shared production workspace responsive for researchers while QC runs.

### 3. Azul-token pacing (`ade_qc` only)

`tdr_qc` needs no Azul token and skips this entirely. For `ade_qc`, before launching a wave the runner
requires the Workspace-Data Azul token to have TTL ≥ **35 min** = `MIN_TOKEN_SECONDS_FOR_RUN` (1800,
the pipeline's own minimum) + a **300 s launch→manifest lag buffer**, so the launched workflow still has
≥ its own minimum when it reaches the Azul step. The token's `gs://` path comes from the workspace
attribute `anvil_data_qc_azul_token`; it is **re-read each check** (no caching), so a mid-run refresh is
picked up, and is validated by `azul_auth.validate_azul_token` (azp + authorized account + remaining
TTL).

If the token is insufficient the runner **pauses new launches but keeps monitoring in-flight**, then:
- **interactive** (TTY, or `--on-stale-token prompt`) → prompt the operator to refresh + re-upload, and
  re-check on Enter;
- **non-interactive** (`--on-stale-token wait`) → poll every 60 s up to 2 h, then give up
  (`RunnerFatalError`).

Backstop: the pipeline's own ≥30-min check makes a token that lapses anyway a **retryable** "token
expired" (exit 2, empty columns), not a spurious QC failure. v1 gates the whole `ade_qc` batch on a
token (< 3 % of datasets are open-access); a per-snapshot `public`-flag skip is a later refinement.

### 4. Continue-on-failure + outcome classification

A single dataset's failure is **recorded and the batch proceeds**; only runner-level fatals (identity
lost, token wait exceeded, bad input/config) stop the run. `Done` ≠ passed — the runner reads the row:

| Outcome | Meaning | Retry? |
|---|---|---|
| `passed` | `<prefix>_qc_passed` true / `<prefix>_qc_exit_code == 0` | — |
| `qc_failed` | pipeline exit 1 (a real QC mismatch) | investigate |
| `safety_abort` | pipeline exit 2 (consent / token / BigQuery preflight) | often retryable |
| `infra_failed` | Cromwell `WorkflowStatus=Failed`, no QC outputs | retry candidate |
| `aborted` | workflow aborted | context-dependent |
| `launch_error` | `create_submission` raised (batch continued) | retry |
| `incomplete` | workflow `Succeeded` but no verdict in the row (unexpected) | investigate |
| `skipped` | already passed (idempotency) | — (use `--force`) |
| `excluded` | marked via the `qc_skip` column (reason shown) | — (use `--run-skipped`) |
| `would_run` | `--dry-run` only | — |

### Skipping known-bad datasets (`qc_skip`)

Some datasets are known ahead of time to be un-runnable through the import-based QC — e.g. those that
exceed the [Terra import ceiling](risk_register.md) (R4) and would run for hours before failing. Rather
than hand-curating the input TSV, mark them **in the data table** (the source of truth) with a **`qc_skip`
column**:

- **Empty / absent** → run normally.
- **Non-empty** → the row is **excluded** (not submitted), and the value *is* the human-readable reason,
  e.g. `too_large_import_ceiling` or `xfail:R4`. One column, self-documenting, sortable/filterable in the
  Terra UI, and durable across runs.

The runner logs each exclusion with its reason and reports them on their own summary line; `excluded` is
**not** a failure (it does not affect the exit code). `--run-skipped` overrides the marker for a run —
kept **independent of `--force`**, so re-running already-passed rows never accidentally launches the
known-bad giants. The `qc_skip` reason is also the natural input to a per-release **QC report** ("what was
not QC'd, and why").

## Submission API contract

Confirmed against the workspace `methodconfigs` listing and the firecloud-orchestration swagger:

- **List / validate:** `GET …/{namespace}/{workspace}/methodconfigs?allRepos=true` → per config
  `{name, namespace, rootEntityType, …}`. For anvil15 the config **namespace = `anvil-developer-work`**
  (the workspace namespace = `terra_billing_project`, *not* Agora's `mbaumann`) and **`rootEntityType =
  anvil15_qc`**.
- **Create:** `POST …/submissions` with a `SubmissionRequest` — `methodConfigurationNamespace`,
  `methodConfigurationName`, `entityType`, `entityName` (= the `snapshot_id`), and **`useCallCache:
  false`** (QC must not reuse cached results), plus a `userComment` for traceability.
- **Monitor:** `GET …/submissions/{id}` → submission `status` (terminal `Done`/`Aborted`) + per-workflow
  `WorkflowStatus` (`Succeeded`/`Failed`/`Aborted`).

## Outputs

- **Log file** (`logs/`, timestamped, labelled `batch-<pipeline>`) — the full record: a header with all
  inputs, redacted API request/response (via the shared `clients.base.BaseClient`), each launch +
  `submissionId` + outcome, concurrency decisions, token status, and the termination cause.
- **stderr / console** — live INFO progress.
- **stdout** — the **end-of-run summary only** (a tally + per-snapshot outcomes), pipe-able like the QC
  commands.
- **Exit codes:** **0** = completed, everything processed passed; **1** = completed, ≥1 failed;
  **2** = runner-level abort.

The Azul token is a **secret** — it is never logged; only the validated metadata (account / azp /
expiry) appears in the log. Per-dataset reports and detailed logs already live in the data-table columns
and in each workflow's own outputs, so the runner does not duplicate them.

## Usage

```bash
# Dry-run: show the effective plan (skip/run cull) for the PriorityA snapshots; create nothing.
qc-batch-runner -p ade_qc --table anvil15_qc -s priorityA.tsv --tier prod --dry-run

# Cap-1 canary: one or two real runs to confirm submissions land + results classify correctly.
qc-batch-runner -p ade_qc --table anvil15_qc -s first_two.tsv --tier prod --max-concurrent 1

# Scale out at the default cap of 5.
qc-batch-runner -p ade_qc --table anvil15_qc -s priorityA.tsv --tier prod --max-concurrent 5

# tdr-qc (no Azul token) for datasets too large for ADE export.
qc-batch-runner -p tdr_qc --table anvil15_qc -s too_large.tsv --tier prod
```

`qc_workspace_name` must be set in the tier config (or passed with `--qc-workspace-name`), e.g.
`anvil-data-qc-shared`.

## Limitations & where human review matters most

- **Orchestration only — it does not deepen the QC.** Each dataset's guarantees are exactly those of the
  underlying pipeline (T1–T2 counts + auth domain; see [`comparison.md`](comparison.md)). Batch scale
  does not add row-identity or cell-value checks.
- **The skip-check is point-in-time and assumes the runner is the sole launcher** during its run. If
  another operator launches the same rows concurrently, the idempotency cull can be stale — coordinate,
  or use `--force` deliberately.
- **Local identity ≠ the workflow's identity.** Pre-flight validates the operator's ADC, but the
  pipeline runs as the workspace pet SA against TDR/BigQuery/export. Trust that path via the **cap-1
  canary** before scaling, not from pre-flight alone.
- **`ade_qc` gates the whole batch on one Azul token.** A lapse pauses launches; a per-snapshot
  `public`-flag skip (to run open-access rows token-free) is deferred.
- **No machine-readable results file yet** — outcomes are in the log/summary and, authoritatively, in
  the data-table columns. A results TSV/JSON is a planned fast-follow.
- **v1 runs locally.** The code is Python-3.10-compatible for a future Terra/WDL deployment, but
  unattended cloud runs (and the non-interactive token-poll mode's real use) are not yet packaged.

After a batch, **triage from the data table**: sort by `<prefix>_qc_passed` / `<prefix>_qc_exit_code`,
and for any non-`passed` outcome open the retained per-dataset workspace and its report before drawing a
conclusion.

## Roadmap (deferred, post-v1)

Sliding-window policy (v2); a machine-readable results file; 3.10 packaging (relax `requires-python`) +
a WDL wrapper for unattended runs; per-snapshot `public`-flag token skip; a `perWorkflowCostCap`.

## References

- Code: [`src/anvil_data_qc/batch/`](../src/anvil_data_qc/batch/) — `cli.py`, `runner.py`, `terra.py`,
  `gcs.py`.
- The QC flows it drives: [`export_import_flow.md`](export_import_flow.md) (`ade-qc`),
  [`tdr_export_flow.md`](tdr_export_flow.md) (`tdr-qc`).
- What the QC checks and its limits: [`comparison.md`](comparison.md).
- The Terra import capacity ceiling (relevant when batching very large datasets):
  [`risk_register.md`](risk_register.md) (R4).
- Service registry: [`services.md`](services.md).
