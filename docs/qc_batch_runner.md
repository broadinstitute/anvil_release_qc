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
> `compare-ingest`) live in the `anvil-release-qc` CLI. `qc-batch-runner` is a deliberately separate CLI a
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

**Settings (reuses [`config.py`](../src/anvil_release_qc/config.py)):** `tier` (default prod), credentials
(ADC; the Terra user is resolved via Sam and logged), the shared workspace's namespace (=
`--billing-project`, default `anvil-developer-work` in prod), and **`--qc-workspace-name`** — the existing
shared QC workspace the runner submits into. The optional local `config/config.yaml` can supply any of
these (e.g. `qc_workspace_name`, a per-tier `adc_credentials_file`) so you don't retype them.

**From the CLI:**

| Option | Meaning |
|---|---|
| `-p, --pipeline` | `ade_qc` or `tdr_qc` (v1). |
| `--table` | Root entity type = the data-table name the method config is bound to (e.g. `anvil15_qc`). |
| `-s, --snapshots` | A **TSV with a `snapshot_id` column** — the rows to process (order-preserving, de-duplicated). Other columns are ignored, so you can export a data-table selection directly. **Required unless `--verify-auth`.** |
| `-t, --tier` | `dev` \| `prod` (defaults to the local config's `default_tier`, else `prod`). |
| `--qc-workspace-name` | The shared QC workspace to submit into (e.g. `anvil-data-qc-shared`); overrides the local config's `qc_workspace_name`. |
| `--billing-project` | Namespace of the shared QC workspace (default `anvil-developer-work` in prod). |
| `--max-concurrent` | Max workflows in flight (default **5**, hard ceiling **10**). |
| `--dry-run` | Validate + print the effective plan; create **no** submissions. |
| `--verify-auth` | Auth/access preflight only: check identity, workspace + method-config access, and (ade_qc) the Azul token, then exit — **no** submissions, `--snapshots` not required. |
| `--force` / `--rerun-passed` | Re-run rows that already passed (default: skip them). |
| `--run-skipped` | Also run rows marked to skip via the `qc_skip` column (default: exclude them). |
| `--on-stale-token` | ade_qc Azul-token handling when TTL is low: `auto` (TTY→prompt, else wait) \| `prompt` \| `wait`. |
| `-c, --config` | Optional local YAML config (default: `config/config.yaml` if it exists, else none). |

## What it does

### 1. Pre-flight (fail-closed, before any launch)

Cheap, reuse-only checks; if any fails the batch stops **before** a single workflow is submitted:

- **Terra-user resolution** on the operator's ADC (`auth.resolve_tier_identity`) — resolved through Sam
  and logged (no allowlist; Terra permissions decide access), the same as the pipelines.
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

**Standalone auth preflight — `--verify-auth`.** For a fast, side-effect-free check *before* preparing a
run, `qc-batch-runner --pipeline … --table … --verify-auth` (no `--snapshots` needed) runs just the
auth/access checks — Terra-user resolution → workspace + method-config access → (ade_qc) Azul-token validity —
prints a `✓ AUTH VERIFIED — operating as <you>; workspace … reachable; …; Azul token …` line, and exits
**0** before any submission. It mirrors the pipelines' `--verify-auth` and is the quickest way to confirm
"am I signed in as the right account, and is my Azul token usable?" before committing to a long batch —
especially handy when you've been switching Google accounts. Note this validates the **runner's** local
auth surface; the pet-SA TDR/BigQuery/export path the *workflows* use is still validated by the **cap-1
canary** (below), not here.

**Credential & access errors are friendly, not tracebacks.** Incomplete setup exits **2** with a clear
message instead of a raw stack trace: missing/expired ADC credentials → "re-run `gcloud auth
application-default login` … and re-save `<adc_credentials_file>`"; and a Terra **403/404** (e.g. an
authorized account that hasn't been granted access to the shared workspace — common when onboarding a new
operator) → "Access denied to workspace `<ns>/<name>` … ask the workspace owner to grant access." Pair with
`--verify-auth` for a fast setup check before a new operator's first run.

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
requires the operator's Azul token to have TTL ≥ **35 min** = `MIN_TOKEN_SECONDS_FOR_RUN` (1800,
the pipeline's own minimum) + a **300 s launch→manifest lag buffer**, so the launched workflow still has
≥ its own minimum when it reaches the Azul step. The token is read from
`gs://<azul_token_bucket>/azul_token/<username>.txt` for the operator running the batch (`--azul-token-bucket`,
same default as the WDL) — the same file the submitted workflows read, since they run as the operator. It
is **re-read each check** (no caching), so a mid-run `azul-login` is picked up, and is validated by
`azul_auth.validate_azul_token` (azp + remaining TTL).

If the token is insufficient the runner **pauses new launches but keeps monitoring in-flight**, then:
- **interactive** (TTY, or `--on-stale-token prompt`) → prompt the operator to re-run `azul-login` (which
  re-uploads), and re-check on Enter;
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

## Running under Terra load

Batch QC creates Terra workspaces and imports data faster than an interactive user, and under load the
shared Terra backend (Rawls + the import service) pushes back. This is the single most important thing to
understand for large batches. **The resulting failures are transient and load-driven — not problems with
your data or the tool** — the pipeline already recovers from most of them, and the rest are handled by
pacing + reruns.

### The mechanism — the pre-created workspace pool

To keep interactive workspace creation fast, Terra keeps a **pool of pre-created workspaces** (each Terra
workspace is heavy — its own Google project, bucket, and security settings). A batch that creates
workspaces faster than the pool refills **drains** it; further creates fall back to slow **from-scratch**
provisioning (minutes) and latency climbs sharply (a "hockey stick"). So the real constraint is your
**workspace-creation _rate_ vs. the pool's refill rate**, not just instantaneous concurrency.

That creation rate is set by **two** factors, not just `--max-concurrent`: it also depends on **how long
each dataset takes**, which scales with the dataset's **tabular-data volume**. Small datasets export and
import quickly, so workspaces are created in rapid succession — a *higher* processing rate that drains the
pool *faster*; large datasets take longer per run, so workspaces are created less often — a *lower* rate,
gentler on the pool. So the same `--max-concurrent` can pressure the backend quite differently depending on
dataset sizes (a batch of many small datasets can push harder than the same cap on large ones). Looking
ahead, the deeper **T3–T4** comparison steps ([comparison.md](comparison.md)) will add per-dataset time in
proportion to data volume, further lowering the processing rate.

### Transient failure signatures (all load-driven, all retryable)

| Symptom | Stage | What it means |
|---|---|---|
| `createWorkspace` → **502 Bad Gateway** (~120 s) | before import | pool drained → slow from-scratch create; a proxy in front of Rawls timed out. The workspace is often created server-side anyway. |
| `createWorkspace` → **500** (~60 s), no workspace | before import | Rawls errored outright under load; nothing was created. |
| import job `Error`: `Rawls.createSnapshotReference … Idle timeout expired: 30000/30000 ms` | import | the import service's 30 s call to Rawls timed out under load (independent of the workspace pool). |

Tell-tale that it's **load, not your data**: the errors are **identical across datasets** and track
**time of day** (worse during business hours, when other users share the backend; the Terra Rawls service
logs show compute-pool starvation — *"CPU is probably starving"* — at those times).

**What the tool handles automatically (v0.3.4+):** on a `createWorkspace` timeout *or* 5xx the pipeline
**confirm-or-adopts** — it checks whether the workspace was actually created and, if so, continues into
the import instead of orphaning it; if it genuinely wasn't created it fails cleanly for a retry
(`_create_workspace_resilient`). The import-stage 30 s timeout lives in Terra's import service (not ours),
so it can't be tuned here — those runs simply retry.

### Operating recipe

- **Pace by time of day.** Business hours → run **`--max-concurrent 1`** (reliable). Off-hours / weekends
  → step up to **2–3** to catch up. High concurrency during peak load is what triggers the symptoms above.
- **Work in small, paced chunks** rather than one giant sweep, letting the pool refill between them.
- **Reruns are the safety net.** Continue-on-failure records each failure and keeps going; on a rerun,
  **idempotency skips** the datasets that already passed and retries only the failures — so you converge by
  re-running the same list until everything is green.
- **Calibrate the safe rate.** Sort a finished batch by launch time and read each row's
  `ade_workspace_create_minutes`: you'll see a **knee** — N fast (~0.4 min) creates, then a jump toward the
  ~2-min ceiling. That N ≈ the pool headroom you had; keep a chunk under it to stay on the fast path. (We've
  observed headroom ≈ 10.)
- **When in doubt,** `--verify-auth` / `--dry-run` first, then a small canary, then scale.

*(Roadmap: a launch-pacing option would let the runner self-limit its creation rate to the pool's refill
rate and adapt to time-of-day load automatically.)*

## Azul reindexing

`ade_qc` exports through the **ADE/Azul** index, and **QC must not run while that index is being
(re)built.** Periodically the UCSC ADE/Azul team **reindexes** a catalog (e.g. `anvil15`) — often to
reprocess data after an indexing-software change. While a catalog is (re)indexing its datasets may be
**empty or incomplete** in Azul, so an export taken then yields a wrong verdict (typically a false FAIL —
the Analysis Workspace comes up short against the unchanged TDR snapshot).

The trap: **the Azul manifest API doesn't signal indexing state** — it returns `200 OK` with an
*empty/incomplete* manifest instead of a "try again later" error. The failure then surfaces late and
misleadingly, deep in the import as `Intermediate batch upsert file … not found, or file was empty` (it
looks like an import bug, not "the source index is mid-rebuild"). UCSC's reindex notices are vague on
timing/scope and the ADE UI only *sometimes* disables Export during a reindex — so **you can't rely on
knowing** a reindex is underway.

**What the tool does (v0.3.5+).** Before exporting, `ade_qc` runs a **reindexing preflight**: it reads
Azul's `GET /health/progress` and, if **either** `unindexed_bundles` or `unindexed_documents` is non-zero,
**fails fast with exit 2** — *before* any export — rather than producing a bad verdict. Exit 2 is a
retryable "cannot run now" abort, not a QC FAIL, so:

- In a **batch**, `qc-batch-runner` records it like any other retryable failure and continues. Because the
  check runs **per dataset**, a reindex that **starts mid-batch** just fails the remaining datasets fast
  (the observed pattern: passing, then failing) — re-run them once indexing finishes and **idempotency**
  retries only those.
- `qc-batch-runner --verify-auth` (ade_qc) makes the **same** check, so you can confirm "not reindexing"
  *before* launching a batch.
- A transient blip reaching `/health/progress` does **not** block a run (fail-open on the check); and if a
  reindex begins in the small window *after* the preflight passes, the import error now carries a hint
  pointing back here.

**Check it yourself** (unauthenticated; prod ADE — dev's Azul URL is in [services.md](services.md)):

```bash
curl -s https://service.explore.anvilproject.org/health/progress | python -m json.tool
#   both unindexed_* == 0  ⇒  safe to QC;   any non-zero  ⇒  wait, then re-run later.
```

**Expect the drain to be uneven.** A reindex's backlog does **not** fall smoothly to zero — it can sit at
a **small non-zero for hours** before dropping to `0` all at once (observed in prod 2026-08-24:
`unindexed_documents` held at `4` for hours, then went straight to `0`). So a small, stuck count **does
not** mean "almost done," and you can't gauge progress by watching the number tick down. Re-check with
`qc-batch-runner --verify-auth` (or the `curl` above) when you're ready to run, rather than guessing.

> **`/health/progress` is deployment-wide, not per-catalog:** a reindex of *any* catalog blocks `ade_qc`
> until it finishes. That over-blocking is deliberate — a false block is just a retry, whereas a false
> *proceed* produces a bad verdict. **`tdr_qc` doesn't use Azul, so it's unaffected.**

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

Sliding-window policy (v2); **launch-pacing** — a min inter-launch delay / adaptive throttle so the runner
self-limits its workspace-creation rate to the pool's refill rate (and eases the manual time-of-day tuning
in [Running under Terra load](#running-under-terra-load)); a machine-readable results file; 3.10 packaging
(relax `requires-python`) + a WDL wrapper for unattended runs; per-snapshot `public`-flag token skip; a
`perWorkflowCostCap`.

## References

- Code: [`src/anvil_release_qc/batch/`](../src/anvil_release_qc/batch/) — `cli.py`, `runner.py`, `terra.py`,
  `gcs.py`.
- The QC flows it drives: [`export_import_flow.md`](export_import_flow.md) (`ade-qc`),
  [`tdr_export_flow.md`](tdr_export_flow.md) (`tdr-qc`).
- What the QC checks and its limits: [`comparison.md`](comparison.md).
- The Terra import capacity ceiling (relevant when batching very large datasets):
  [`risk_register.md`](risk_register.md) (R4).
- Service registry: [`services.md`](services.md).
