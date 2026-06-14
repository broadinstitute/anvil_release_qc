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
| **Auth domain** | the workspace's authorization domain(s) match the snapshot's (Sam) — critical for controlled-access. |

Both the harmonized **`anvil_*` (Findability Subset)** tables and the **submitter's** study tables are
compared (all by identical name, 1:1). The primary key is `<table>_id` (the first column).

## Normalizations (expected differences, not flagged)

The Terra import legitimately transforms the data; these are normalized so they do not cause false
failures:

- **Empty source tables/columns are dropped on import.** TDR snapshots routinely carry data-model
  tables/columns that are empty for a given dataset. The TDR baseline is therefore computed on
  **non-empty** tables (row count > 0) and **non-empty** columns (≥1 non-null value; for arrays, ≥1
  non-empty array). Empty TDR tables are *expected* to be absent from the AW.
- **TDR system tables are excluded.** Every snapshot carries a `datarepo_row_ids` plumbing table that
  is intentionally never exported; it (and anything in `ComparisonProfile.ignore_tables`) is skipped.
- **Column renames/additions are tolerated by the count check.** Terra prefixes attributes with
  `pfb:`, derives the entity id as `<entitytype>_id`, adds **reference** columns from the snapshot's
  foreign keys, and surfaces file references as `drs_uri`. Column *names* are not compared in this
  tier — only that no non-empty column was lost (AW count ≥ TDR non-empty count).

## Running it

The comparison runs automatically as the final step of the `qc` pipeline (after the import). To QC an
**existing** workspace without re-importing:

```bash
anvil-data-qc compare \
  --snapshot-id <tdr-snapshot-id> \
  --workspace-namespace <terra-billing-project> \
  --workspace-name <workspace-name> \
  --tier dev
```

It prints the report and exits non-zero on failure (for batch/WDL gating).

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
- **Auth domain on controlled-access** has so far been validated only against an open-access dataset
  (1000G — empty domain on both sides). **→ Confirm the check on a controlled dataset before relying
  on it for compliance.**

These deferrals are intentional under release time pressure; the engine's seams (`TabularDataSource`,
`ComparisonProfile`, `compare`) are built so each is an additive enhancement, not a rewrite.
