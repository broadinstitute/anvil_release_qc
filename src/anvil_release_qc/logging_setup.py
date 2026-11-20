"""Logging setup and the shared request/response logging used by every HTTP layer.

A header at the top of the log, one log line per request/response with secrets redacted (see
``redaction``), and the response's correlation/trace ids so the matching server-side logs can be found.
The detailed log file is intended to be a workflow (WDL) output.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from anvil_release_qc.redaction import redact_text

# Correlation/trace headers worth logging (paste a trace id into Cloud Logging to find the request).
CORRELATION_HEADERS = ("x-cloud-trace-context", "traceparent", "x-request-id")

LOGGER_NAME = "anvil_release_qc"


def setup_logging(
    log_dir: Path, tier: str, snapshot_id: str, verbose: bool = True
) -> tuple[logging.Logger, Path]:
    """Configure the logger to write to both the console and a timestamped detailed log file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_snapshot = "".join(c if (c.isalnum() or c in "-_") else "_" for c in snapshot_id)
    log_file = log_dir / f"anvil_release_qc_{tier}_{safe_snapshot}_{ts}.log"

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    return logger, log_file


def log_run_start(logger: logging.Logger, **fields: object) -> None:
    """Log one line at the top of the log with the run's parameters (version, tier, snapshot, ...)."""
    logger.info("anvil-release-qc run: %s", ", ".join(f"{key}={value}" for key, value in fields.items()))


def log_request(logger: logging.Logger, method: str, url: str, params=None, body=None) -> None:
    """Log a request at DEBUG with secrets redacted. Headers are not logged (only Authorization is
    sensitive, and the rest is boilerplate)."""
    logger.debug("%s %s", method, redact_text(url))
    if params:
        logger.debug("  params: %s", redact_text(str(params)))
    if body is not None:
        logger.debug("  body: %s", redact_text(str(body)))


def log_response(logger: logging.Logger, resp) -> None:
    """Log a response's status and any correlation/trace ids at DEBUG."""
    lower = {k.lower(): v for k, v in dict(resp.headers).items()}
    corr = {h: lower[h] for h in CORRELATION_HEADERS if h in lower}
    logger.debug("  -> %s %s%s", resp.status_code, resp.reason or "", f"  {corr}" if corr else "")
