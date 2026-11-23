# AW ↔ TDR snapshot table comparison (QC)

After a dataset is exported into an Analysis Workspace (AW), this QC step verifies the AW data tables
faithfully reflect the source **TDR snapshot** — that nothing was lost in the ADE→AW export/import.
The TDR snapshot is the **source of truth**; the AW is the candidate.

Implemented and validated against the dev **1000G** dataset (`compare` reports **PASS**).

## What it checks (tiers 1–2, now)

Per table, comparing the TDR snapshot (read from **BigQuery**) to the AW (read from **Rawls**
`GET …/entities`):

| Check | Rule |
|---|---|
| **Table parity** (strict 1:1) | every non-empty TDR table has a same-named AW table, **and** every AW table has a same-named TDR table. A *missing* table (data lost) **or** an *AW-only* table both **fail**. |
| **Row counts** | TDR row count == AW row count, per table. |
| **Column-count sanity** | AW column count **≥** TDR non-empty column count. Columns may be *added* by Terra; none may be *dropped*. |
| **Skipped tables** (not compared, never a failure) | `file_inventory` (never imported into the AW) and any TDR table with **more than 350,000 rows** (too large for Terra to import). Each is named in the report with a `SKIPPED — <table>: <reason>` line. |
| **Auth domain** | the workspace's authorization domain(s) match the snapshot's (Sam) — critical for controlled-access. |

Both the harmonized **`anvil_*` (Findability Subset)** tables and the **submitter's** study tables are
compared (all by identical name, 1:1). The primary key is `<table>_id` (the first column).

## Data-volume metrics (table / row / value counts)

Alongside the comparison, the flows report three counts describing **how much tabular data the snapshot
carries** — an instrument for understanding and managing Terra workspace Data Table capacity (which has
no hard limit but degrades as volume grows, especially for high-volume clinical/EHR datasets). Computed
from the TDR snapshot's BigQuery **structural metadata** (`snapshot_data_volume` in `bigquery.py`):

| Metric | Definition |
|---|---|
| `total_table_count` | number of tables |
| `total_row_count` | Σ rows over all tables |
| `total_value_count` | Σ (rows × columns) per table |

**Counted set = non-system, non-empty tables** — *the same set this comparison treats as the TDR
baseline* before it skips tables the import does not carry (`file_inventory` and tables over the
350,000-row import limit — see [Normalizations](#normalizations-expected-differences-not-flagged)). So
**`total_table_count` equals the "N tables compared"** above **plus** any skipped tables:
- **system tables excluded** (e.g. `datarepo_row_ids` — TDR plumbing, never imported; it would roughly
  *double* the counts), and
- **empty tables excluded** (`row_count == 0`) — Terra does not import them, so from the Data Tables'
  perspective they are not "real" tables.

**`total_value_count` uses *schema* columns** (a table's full column count), making it a deliberate,
**modest over-approximation** of the value volume Terra actually ingests: Terra also drops *empty
columns* on import, but counting them avoids an expensive per-column scan — exactly what we avoid on the
large datasets these metrics target. (The two queries — `INFORMATION_SCHEMA.COLUMNS` + a `COUNT(*)` per
table — do not scan column values, so they stay fast even on huge snapshots.) When column/value
comparison lands, this can be tightened to non-empty columns.

**Computed early** (before the export/import) and logged at the **start** of a run — so they double as
an operator heads-up (volume → expected stage durations → likelihood of trouble) and are **available
even if the import later times out or errors** (the failure case where knowing the size matters most).

**Surfacing & scope:** the canonical line `Data volume (TDR snapshot) — total_table_count=… |
total_row_count=… | total_value_count=…` (comma thousands separators for readability) appears in the
**detailed log** and the **QC report**, and as the `Int` workflow outputs `total_table_count` /
`total_row_count` / `total_value_count` (commas stripped). Reported by **`ade-qc`, `tdr-qc`, and
`compare`**. **Not** `compare-ingest` — it is intentionally a one-directional, submitter-driven *subset*
check, so full-snapshot counts there would be misleading.

## Terra Data Table import ceiling (observed 2026-06)

These metrics exist because some datasets are **too large for Terra to import into Data Tables at all**.
The largest anvil14 clinical/EHR dataset — **17 tables, ~13.1M rows, ~142M values** — **failed to import
even after ~11 h 42 m**. Terra's import service (cWDS) enforces an effective **~6 h job TTL** via a
periodic reaper scan, giving a real-world ceiling of roughly **6–12 h** depending on when a job starts
relative to the scan; this dataset exceeded even the long end. The import is **transactional** — on
failure the workspace Data Tables are left **empty** (no partial import), and the `importJob` reports
`status: "Error"` ("Job failed to complete in 6 hours").

What this means for QC:
- `ade-qc` / `tdr-qc` compare the AW **after** the import, so a dataset that won't import has **no
  populated workspace to compare** — the AW-vs-snapshot check simply cannot run for it. The **data-volume
  metrics still report** (they are computed before the import), so a failed row still shows *how big* the
  dataset was.
- **`compare-ingest` (ADSW → TDR, no import) is unaffected** and remains the QC coverage for such datasets.
- The tool's `importJob` wait is capped at **2 h** (`wait_for_import`); past that it reports a timeout with
  partial stage durations. Even an unbounded wait would hit Terra's ~6–12 h reaper, so raising it would
  **not** import these datasets — the ceiling is Terra's, not the tool's. Tracked as risk **R4**.

## Normalizations (expected differences, not flagged)

The Terra import legitimately transforms the data; these are normalized so they do not cause false
failures:

- **Empty source tables/columns are dropped on import.** TDR snapshots routinely carry data-model
  tables/columns that are empty for a given dataset. The TDR baseline is therefore computed on
  **non-empty** tables (row count > 0) and **non-empty** columns (≥1 non-null value; for arrays, ≥1
  non-empty array). Empty TDR tables are *expected* to be absent from the AW.
- **TDR system tables are excluded.** Every snapshot carries a `datarepo_row_ids` plumbing table that
  is intentionally never exported; it (and anything in `ComparisonProfile.tdr_system_tables`) is skipped.
- **Import-added AW tables are excluded.** The PFB/ADE import synthesizes a few workspace entity types
  that have **no TDR-snapshot counterpart** — e.g. `duos_dataset_registration`, the DUOS (Data Use
  Oversight System) registration record (columns all `pfb:`-prefixed), present for DUOS-registered
  datasets. These (`ComparisonProfile.import_added_aw_tables`) are skipped; otherwise strict 1:1 parity
  would flag them as unexpected "AW-only" tables. The report names the two exclusion categories — TDR
  system tables vs import-added AW tables — separately, by provenance.
- **`file_inventory` is skipped.** It is a TDR table that is never imported into the AW, so there is
  nothing to compare (`ComparisonProfile.not_imported_tables`). The report notes it:
  `SKIPPED — file_inventory: not imported into the AW by design, so not compared (not a failure)`.
- **Tables over 350,000 rows are skipped.** Terra's Data Table import cannot take a table that large,
  so any TDR table with **more than** 350,000 rows (`ComparisonProfile.max_importable_table_rows`) is
  left out of the comparison on both sides — whether or not the AW has a table of that name — and does
  **not** fail the QC. The report names each with its TDR row count, e.g.
  `SKIPPED — observation: 1,204,331 rows in TDR exceeds the 350,000-row Terra import limit, so it cannot
  be imported and was not compared (not a failure)`.
- **Column renames/additions are tolerated by the count check.** Terra prefixes attributes with
  `pfb:`, derives the entity id as `<entitytype>_id`, adds **reference** columns from the snapshot's
  foreign keys, and surfaces file references as `drs_uri`. Column *names* are not compared in this
  tier — only that no non-empty column was lost (AW count ≥ TDR non-empty count).

## Running it

The comparison runs automatically as the final step of both the `ade-qc` and `tdr-qc` pipelines (after
the import) — the comparison engine is identical regardless of how the workspace was populated. To QC an
**existing** workspace without re-importing:

```bash
anvil-release-qc compare \
  --snapshot-id <tdr-snapshot-id> \
  --workspace-namespace <terra-billing-project> \
  --workspace-name <workspace-name> \
  --tier dev
```

It prints the report and exits non-zero on failure (for batch/WDL gating).

## Ingestion QC (ADSW → TDR): `compare-ingest`

The earliest and most fundamental QC of the pipeline checks the **first** hop —
`Submitter Workspace (ADSW) → TDR Snapshot` — to confirm that **every table the submitter provided was
ingested into the system of record** with the same row count. (TDR is the system of record; the
submitter workspace is the reference here.) This is typically run **repeatedly**, as submitters add
missing data and correct prior submissions.

It compares the submitter (ADSW) workspace (read from **Rawls**) to the TDR snapshot (read from
**BigQuery**), **driven from the submitter side**:

| Check | Rule |
|---|---|
| **Table coverage** | every submitter table has a same-named TDR table (a *missing* one = **not ingested** = fail). |
| **Row counts** | submitter row count **==** TDR row count, per table (exact, 1:1). |

TDR's additional tables — the harmonized **`anvil_*` Findability Subset**, `file_inventory`,
`workspace_attributes`, and the `datarepo_row_ids` system table — have no submitter counterpart and are
**out of scope by construction** (the comparison only iterates the submitter's table set, so it stays
correct as the FSS schema grows). There is **no PFB export** in this flow — no Azul, no signed URL, no
consent gate; it reads table/row counts only.

```bash
anvil-release-qc compare-ingest \
  --submitter-workspace-namespace <terra-billing-project> \
  --submitter-workspace-name <submitter-workspace-name> \
  --snapshot-id <tdr-snapshot-id> \
  --tier dev
```

It prints the report and exits non-zero on failure (for batch/WDL gating).

**Scope now / deferred:** like the AW comparison, this starts at **counts** — table presence + exact
row-count parity. **Column comparison is a deliberate fast-follow**: ingestion maps/harmonizes the
submitter's columns into the FSS, so the column rule is best designed against real column data to avoid
false positives. Row *identity* (T3) and cell values (T4) remain deferred. **→ Until columns are
compared, confirm that the submitter columns you care about are present in TDR.**

## Limitations & where human review matters most

This tier is a **count-level** check: deliberately conservative — it won't cry wolf — but it does
**not** verify everything. Know what it does *not* catch, and review those by hand in the retained
workspace:

- **Specific column identity is not checked (deferred).** Because only column *counts* are compared, a
  specific non-empty TDR column could be dropped unnoticed if Terra added other columns that keep the
  count ≥. Full column-name comparison (with the `pfb:`/rename mapping) is a deferred focused pass.
  **→ Review that the columns you care about are present.**
- **Row identity is not checked (deferred, T3).** Equal row *counts* do not prove the *same* rows are
  present. **→ For high-stakes datasets, spot-check that key ids match.**
- **Cell values are not compared (deferred, T4).** No value-level comparison is done; type
  representation, DRS/file-reference forms, arrays, and null-vs-empty are not verified. **→ Spot-check
  values, and that DRS/file references resolve.**
- **Skipped tables are not verified at all.** `file_inventory` and every table over 350,000 rows are
  left out of the comparison (see [Normalizations](#normalizations-expected-differences-not-flagged)),
  so a PASS says nothing about them. **→ Check the report's `SKIPPED` lines; for large tables, rely on
  `compare-ingest` (ADSW → TDR, no import) for their row-count coverage.**
- **Very large datasets may exceed Terra's Data Table import capacity — the AW comparison then can't run.**
  `ade-qc` / `tdr-qc` compare the AW *after* the import; if the import can't complete (see
  [Terra Data Table import ceiling](#terra-data-table-import-ceiling-observed-2026-06)), there is no
  workspace to compare. **→ For such datasets, rely on `compare-ingest` (ADSW → TDR, no import) plus the
  data-volume metrics; the AW-vs-snapshot check is unavailable until the data can be imported.**

(The **auth-domain check** *is* validated against real controlled-access datasets — a non-empty domain via
`ade-qc` on 2026-06-26 and `tdr-qc` on the prod `ANVIL_dGTEx_GRU_v1` snapshot on 2026-06-27 — so it is no
longer a limitation; see the auth-domain row under "What it checks".)

These deferrals are intentional under release time pressure; the engine's seams (`TabularDataSource`,
`ComparisonProfile`, `compare`) are built so each is an additive enhancement, not a rewrite.
