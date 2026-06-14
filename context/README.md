# context/

Raw, working **contextual inputs** for this project — schemas, screenshots, captured API responses,
and design notes that inform the code and documentation.

## Status: not authoritative

This directory is a **staging area**, not the source of truth. The authoritative, maintained
references live in [`../docs/`](../docs/) and [`../CLAUDE.md`](../CLAUDE.md); raw inputs land here
first and are **distilled into `docs/` and the code over time**. When the two disagree, `docs/` wins —
material here may be a point-in-time snapshot or superseded.

## What belongs here

- Authoritative-source **pointers** (URLs to OpenAPI specs, schema files, repos) — preferred over
  paraphrase, so the current truth can be re-fetched and verified.
- **Concrete real examples** that collapse ambiguity: a screenshot, a (scrubbed) real API response, a
  specific snapshot/workspace id to query.
- **Decisions & semantics** that can't be inferred from code — what is the source of truth, which
  differences are expected vs. bugs, and the *why* behind them.

Keep secrets out (see `../docs/capturing_http_traffic.md` for scrubbing captures); large captures
belong in gitignored `../tmp/`, not here.

## Conventions

- **Date-stamp** time-sensitive artifacts in the filename: `..._YYYYMMDD` (e.g.
  `anvil_azul_1000G_aw_data_tables_20260611.jpg`).
- Start each note with a one-line header: **source · date · tier/dataset · status**
  (`current` / `snapshot` / `superseded`).
- Prefer linking the authoritative source over copying it; if you copy, say where it came from.
- Be deliberate with binaries (screenshots): a few are fine; if they multiply, prefer Git LFS or an
  external link to keep history lean.

## Distillation

Treat entries as inputs to be folded into the durable docs. When context here is incorporated into
`docs/`, `CLAUDE.md`, the [risk register](../docs/risk_register.md), or code, note where it went (or
remove/mark the entry) so future readers aren't left guessing what is still relevant.

## Current contents

- `terra_workspace_data_tables.md` — Terra Data Tables model (entity/attribute/relationship; Rawls
  entity endpoints). → distilled into [`../docs/comparison.md`](../docs/comparison.md).
- `comparing_tdr_terra_tabular_data.md` — TDR-vs-Terra import behaviors (empty source tables/columns
  are dropped). → distilled into [`../docs/comparison.md`](../docs/comparison.md).
- `anvil_azul_1000G_aw_data_tables_20260611.jpg` — screenshot of the 1000G AW data tables (dev).
