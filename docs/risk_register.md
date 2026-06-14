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
| R7 | Azul controlled-access auth for headless/batch | Medium (manual path works) | Understood — APAT index-only *by design*; interactive token + `azul-login` (built); native-client ask = security-neutral long-term fix |
| R8 | Controlled-access PFB signed-URL mishandling → Federal DMI | High (compliance) | Mitigated — fail-closed consent gate (NRES-only); controlled-access blocked pending security review |

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
id. The gcloud ADC token (`azp` = gcloud) is rejected (401), and so is the Azul **PAT** (APAT).
**Resolved understanding (2026-06-11):** the APAT being rejected for controlled-access is **by design,
not a bug** — the Azul team deliberately scoped the PAT to long-term access to Azul's *index* content
and **disallowed using it to reach controlled-access data in TDR**, to protect that data. We will
**not** ask them to weaken that.
**Working approach:** use the **interactive Google access token** (from the Azul Browser or Swagger
login; `azp` = Azul's client, ~1 h, **non-refreshable**). It is used only in the **short** Azul phase
(minutes); the long import phase uses the **refreshable** gcloud ADC token, so mid-run expiry is only
a small Azul-phase risk. Ergonomics (**implemented 2026-06-11**): the `azul-login` CLI helper captures
+ `tokeninfo`-validates the token (`azp`, account email, `exp`) and writes the token file, and a
pre-run gate fails fast if the Azul token is wrong-client/wrong-account/near-expiry. (Not yet run
end-to-end for a controlled-access dataset.) One human login (~hourly) covers a large batch, since one
token can launch many concurrent pipelines.
**Preferred long-term fix (security-neutral — FUTURE ASK to Azul):** ask Azul to **accept a
native/installed OAuth client for CLI login** (PKCE + loopback, no client secret). This would enable
an unattended `gcloud auth login`-style flow **without** weakening controlled-access protection — it
is still a real per-user Google login, still a per-user ~1 h token, still no long-lived TDR access. It
is a *different* ask than changing the PAT (which we are not requesting). Until then, the manual
`azul-login` path stands.
**Revisit:** if/when we make the native-client ask; before any controlled-access batch run at scale.

### R8 — Controlled-access PFB signed-URL mishandling (Federal DMI)
**Impact:** The Azul PFB signed URL grants direct access to **all of an NIH study's controlled-access
tabular data**. Mishandling it — e.g. importing a **production** signed URL into the **insecure dev
tier**, or any unintended destination — would be a **Federal Data Management Incident**. As a
long-lived, multi-operator tool, human/config error over time is a real risk even when the current
code is correct.
**Mitigation (2026-06-14):** a **fail-closed consent gate** (`src/anvil_data_qc/safety.py`): the
pipeline proceeds only when the TDR snapshot's top-level `consentCode` is exactly `"NRES"`
(open-access). Empty/unknown/any other value is **blocked, in every tier, before any signed URL is
generated**. The pipeline is **not yet approved for controlled-access data** — that requires a
security-team review of the end-to-end flow. Until then it serves the 13 open-access prod datasets.
**Revisit:** Only after the security review, before enabling controlled-access; re-affirm the gate is
intact on each release.
