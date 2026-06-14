"""Tests for the stage-timing helper (anvil_data_qc.timing)."""

import logging
import time

import pytest

from anvil_data_qc.logging_setup import LOGGER_NAME
from anvil_data_qc.timing import stage_timer


def test_stage_timer_records_and_logs(caplog):
    durations: dict[str, float] = {}
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with stage_timer("mystage", durations):
            time.sleep(0.01)
    assert isinstance(durations["mystage"], float) and durations["mystage"] >= 0.0
    assert any("mystage" in record.message for record in caplog.records)


def test_stage_timer_records_even_on_exception():
    durations: dict[str, float] = {}
    with pytest.raises(ValueError):
        with stage_timer("boom", durations):
            raise ValueError("boom")
    assert "boom" in durations  # recorded in `finally`, even when the stage fails
