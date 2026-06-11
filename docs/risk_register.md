# Risk Register

A short, living list of the most significant risks to this project. Update **Status** as risks are
mitigated, retired, or newly identified, and note the **Revisit** trigger so each risk gets a
deliberate re-check at the right moment. Severity reflects potential impact × likelihood.

Initial assessment: **2026-06-10** (design phase, pre-implementation). The single most effective
risk reducer right now is a thin end-to-end spike against one real snapshot — most of R1 (and part
of R2) is retired by *doing* rather than planning.

| ID | Risk | Severity | Status |
|----|------|----------|--------|
| R1 | Export/import mechanics assumed, not verified | High | **Retired (open-access)** — full flow validated end-to-end on dev incl. import→Done |
| R2 | Controlled-access correctness (auth domain; dev/prod parity) | High (compliance) | Model resolved — import auto-applies; QC verifies post-import |
| R3 | Comparison fidelity: expected vs. unexpected differences | High | Deferred (by design) |
| R4 | Batch-scale robustness & retained-workspace cost/quota | Medium | Watching |
| R5 | Multi-org API drift / reverse-engineered UI flow | Medium (rising over time) | Mitigating |
| R6 | Premature generalization in our own design | Medium | Watching |
| R7 | Azul controlled-access auth for headless/batch | High (blocks most datasets in batch) | **Open** — gcloud + APAT both rejected; with Azul team |

---

### R1 — Export/import mechanics assumed, not verified
**Impact:** Concentrates almost all schedule risk. Unknowns: whether a TDR Snapshot ID is the actual
Azul filter that yields the PFB; the Azul async manifest job/poll + signed-URL protocol and PFB
format token; the Terra import job's polling/error/limits and any intermediate calls; whether one
Google bearer token satisfies the *programmatic* Azul + Terra import paths (vs. browser/cookie auth).
**Mitigation:** Capture real interactive traffic (HAR) → build the thinnest end-to-end spike against
one real snapshot before polishing.
**Update (2026-06-10):** Mechanics **verified** from a real capture and documented in
[`export_import_flow.md`](export_import_flow.md): Azul `PUT …/manifest/files?…&format=verbatim.pfb`
(async 301→302 → signed S3 URL); Terra `createWorkspace` → `importJob {url, filetype:"pfb"}` (async).
Residual risk is implementation + a few parameterizations (e.g. the versioned Azul `catalog`).
**Update (2026-06-11):** Full flow **run end-to-end against dev for an open-access snapshot** — TDR →
BigQuery (`anvil_dataset.dataset_id`) → Azul manifest (anonymous) → `createWorkspace` (201) →
`importJob` polled to `Done`. importJob shapes confirmed (`jobId`; status lifecycle
Pending→…→ReadyForUpsert→Upserting→Done/Error). **Open-access path retired**; the remaining gap is
controlled-access Azul authentication — see **R7**.
**Revisit:** When R7 (controlled-access auth) is resolved; otherwise retired for the open-access path.

### R2 — Controlled-access correctness (auth domain; dev/prod parity)
**Impact:** Workspaces hold controlled-access tabular metadata that must be protected by an auth
domain. **Resolved model (2026-06-10 capture):** the workspace is created with an empty
`authorizationDomain`, then the **import service privileged-sets the snapshot's auth domain(s) after
creation** — so the automation must **verify** it, not set it. Residual risk: the import silently
failing to apply the auth domain (→ under-protected controlled data), and the dev tier lacking the
controlled-access datasets/groups present in prod (riskiest path least tested).
**Mitigation:** After import **success**, assert workspace auth domain(s) == snapshot auth domain(s)
(Sam `…/datasnapshot/{id}/authDomain` vs Rawls workspace `authorizationDomain`); treat empty/mismatch
on a controlled dataset as a QC failure. Exercise the controlled-access path before prod.
See [`export_import_flow.md`](export_import_flow.md) §6 and its QC section.
**Revisit:** When implementing auth-domain verification; before first prod run.

### R3 — Comparison fidelity: expected vs. unexpected differences
**Impact:** Where the project's value and difficulty concentrate. False positives erode trust in QC;
false negatives defeat its purpose. Representation mismatches between BigQuery (typed, nulls, arrays,
nested) and Rawls entity attributes (JSON-ish), and especially DRS/file-reference form, may make
"nearly verbatim" subtler than prefix + admin columns.
**Mitigation:** Design profile-driven comparison with real dataset examples; decide comparison depth
deliberately (metadata+counts vs. row-level value diffing).
**Revisit:** When the comparison engine is taken up (deferred until resources permit).

### R4 — Batch-scale robustness & retained-workspace cost/quota
**Impact:** Across hundreds of datasets, transient failures/rate-limits/partial state are normal.
Mid-run failure leaves a half-created workspace. Retained workspaces accumulate → Terra
billing-project quota + ongoing cost + an eventual cleanup/lifecycle question — **more so now** that
workspace names are minute-granular (a new workspace per run, including reruns).
**Decision (2026-06-10):** workspace timestamp is minute-granular (`…_YYYYMMDDHHMM`) so batch reruns
don't collide on an existing workspace (the primary use case); the earlier per-day + fail-fast scheme
suited interactive use but blocked same-day reruns. Trade-off: faster workspace accumulation.
**Mitigation:** Plan a cleanup/lifecycle path for accumulated and failed-run workspaces; track
workspace count/cost; revisit idempotency/resume once real rerun patterns are seen.
**Revisit:** After the first batch run.

### R5 — Multi-org API drift / reverse-engineered UI flow
**Impact:** We mirror an interactive flow that may use endpoints not intended as stable public APIs,
maintained independently by UCSC (Azul) and Broad (Terra/TDR). The HAR is point-in-time; the flow
can shift underneath us with no contract guarantee → risk of silent breakage over the multi-year
horizon.
**Mitigation (partly in place):** Detailed request/response logging; service version/status header;
mirror UI calls; scrubbed-HAR replay fixtures. Consider a small smoke/contract check later.
**Revisit:** Periodically; on any unexplained breakage.

### R6 — Premature generalization in our own design
**Impact:** Designing interfaces (TabularDataSource, comparison profiles, pluggable strategies)
before the first concrete path teaches us what they should be risks abstractions that don't fit
reality. Related: logic drifting into notebooks instead of the package.
**Mitigation:** Implement AW-vs-snapshot concretely first; let the interface emerge from one working
case, then generalize. Keep notebooks thin (import the package; no logic).
**Revisit:** Continuously during implementation; before committing shared interfaces.

### R7 — Azul authentication for controlled-access (headless/batch)
**Impact:** Most AnVIL datasets are controlled-access and the primary use is batch — but Azul's
registered-user validation requires a Google access token whose `azp` equals Azul's own OAuth client
id. The gcloud ADC token (`azp` = gcloud) is rejected (401), and an APAT from `GET /user/token` is
*also* rejected ("Invalid credentials"; `jti:0`). The interactive flow uses an authorization-code
token (correct `azp`) issued BFF-style with **no refresh token** — awkward for headless/batch. So
controlled-access export is currently **blocked** for automation; open-access (anonymous) is fine.
**Mitigation / next:** Azul-team question — how should a headless/batch client obtain an Azul-accepted
user token (is the APAT meant to be exchanged for an access token; is there a service-account path; is
APAT support complete in dev)? See [`export_import_flow.md`](export_import_flow.md) → Authentication.
**Revisit:** When the Azul team responds; before any controlled-access run.
