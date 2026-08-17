"""qc_batch_runner: automated batch execution of the QC pipelines over a snapshot list.

A "meta-pipeline" that drives the Terra submissions API to run the existing ``AdeQc``/``TdrQc`` method
configs on data-table rows in a shared QC workspace, capped at a small concurrency to protect shared
production. It orchestrates the workflows; the per-dataset QC verdicts land in the data-table columns as
usual. Separate CLI from ``anvil-data-qc`` (``qc-batch-runner``); see ``batch.cli`` and ``batch.runner``.

Written to be Python 3.10-compatible so it can also run in Terra Cloud (Jupyter) environments.
"""
