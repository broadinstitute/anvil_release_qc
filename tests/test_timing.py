"""Tests for the stage-timing helper (anvil_data_qc.timing)."""

import logging
import time

import pytest

from anvil_data_qc.logging_setup import LOGGER_NAME
from anvil_data_qc.timing import format_stage_summary, log_stage_summary, stage_timer


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


def test_format_stage_summary_minutes_one_decimal():
    # Seconds -> minutes (1 decimal), run order preserved, total included, parseable name=value pairs.
    line = format_stage_summary({"tdr_export": 41.8, "workspace_create": 21.4, "import": 618.0})
    assert line == (
        "Stage timings (minutes) — total 11.4: tdr_export=0.7, workspace_create=0.4, import=10.3"
    )


def test_format_stage_summary_single_stage():
    # The dry-run case: only the export stage recorded.
    assert (
        format_stage_summary({"tdr_export": 90.0})
        == "Stage timings (minutes) — total 1.5: tdr_export=1.5"
    )


def test_log_stage_summary_emits_one_line(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_stage_summary({"azul_manifest": 2.0, "workspace_create": 21.4, "import": 600.0})
    summaries = [r.message for r in caplog.records if "Stage timings (minutes)" in r.message]
    assert len(summaries) == 1
    assert "import=10.0" in summaries[0]


def test_log_stage_summary_noop_on_empty(caplog):
    # A run that aborts before any stage (empty durations) emits no summary line.
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        log_stage_summary({})
    assert not any("Stage timings" in r.message for r in caplog.records)
