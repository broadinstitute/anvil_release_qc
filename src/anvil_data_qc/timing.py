"""Lightweight stage timing.

A small context manager to measure how long a pipeline stage takes: it logs the duration as the stage
finishes (so early stages report before a long one runs) and records it into a dict for the run's
output report. These durations give ops visibility under load and inform the user-facing
time-remaining messaging.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .logging_setup import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


@contextmanager
def stage_timer(name: str, into: dict[str, float]) -> Iterator[None]:
    """Time the wrapped block: record its elapsed seconds in ``into[name]`` and log on completion."""
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        into[name] = elapsed
        logger.info("Stage '%s' took %.1fs", name, elapsed)
