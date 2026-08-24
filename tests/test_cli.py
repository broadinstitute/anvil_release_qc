"""CLI wiring tests (Typer) — flags plumb through to the pipeline functions (pipeline is mocked)."""

from typer.testing import CliRunner

from anvil_data_qc import cli
from anvil_data_qc.azul_auth import AzulReindexingError

runner = CliRunner()


def _capture(monkeypatch, attr, result):
    """Replace ``cli.<attr>`` with a stub that records its kwargs and returns ``result``."""
    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(cli, attr, fake)
    return captured


def test_tdr_qc_verify_auth_flag_plumbs_through(monkeypatch):
    captured = _capture(monkeypatch, "run_tdr_qc", {"verify_auth": True})
    res = runner.invoke(cli.app, ["tdr-qc", "-s", "snap-1", "--tier", "prod", "--verify-auth"])
    assert res.exit_code == 0  # verify-auth result has no qc_passed -> clean exit
    assert captured["verify_auth"] is True
    assert captured["snapshot_id"] == "snap-1"


def test_tdr_qc_verify_auth_defaults_false(monkeypatch):
    captured = _capture(monkeypatch, "run_tdr_qc", {"qc_passed": True})
    res = runner.invoke(cli.app, ["tdr-qc", "-s", "snap-1"])
    assert res.exit_code == 0
    assert captured["verify_auth"] is False


def test_ade_qc_verify_auth_flag_plumbs_through(monkeypatch):
    captured = _capture(monkeypatch, "run_ade_qc", {"verify_auth": True})
    res = runner.invoke(cli.app, ["ade-qc", "-s", "snap-1", "--verify-auth"])
    assert res.exit_code == 0
    assert captured["verify_auth"] is True


def test_ade_qc_verify_auth_defaults_false(monkeypatch):
    captured = _capture(monkeypatch, "run_ade_qc", {"qc_passed": True})
    res = runner.invoke(cli.app, ["ade-qc", "-s", "snap-1"])
    assert res.exit_code == 0
    assert captured["verify_auth"] is False


def test_ade_qc_reindexing_exits_2(monkeypatch):
    def boom(**_kwargs):  # Azul (re)indexing detected in the pipeline preflight
        raise AzulReindexingError("Azul is (re)indexing (unindexed_bundles=5)")

    monkeypatch.setattr(cli, "run_ade_qc", boom)
    res = runner.invoke(cli.app, ["ade-qc", "-s", "snap-1"])
    assert res.exit_code == 2  # a retryable "cannot run now" abort, not a QC verdict
