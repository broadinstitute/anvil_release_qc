"""AW-vs-TDR-snapshot tabular comparison (the QC verification layer).

A `TabularDataSource` exposes a uniform per-table inventory; the engine compares a reference (the TDR
snapshot, via BigQuery) against a candidate (the Analysis Workspace, via Rawls) under a
`ComparisonProfile` of expected differences. Today the checks are T1-T2 (table parity, row counts,
column-count sanity) plus auth-domain verification; T3 (id-set parity) and T4 (cell values) extend
the same seams additively. See docs/comparison.md.
"""
