"""CLI wiring tests (Typer) — flags plumb through to the pipeline functions (pipeline is mocked)."""

from typer.testing import CliRunner

from anvil_release_qc import cli
from anvil_release_qc.azul_auth import AzulReindexingError

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


def test_billing_flags_plumb_through(monkeypatch):
    for cmd, attr in (("ade-qc", "run_ade_qc"), ("tdr-qc", "run_tdr_qc")):
        captured = _capture(monkeypatch, attr, {"qc_passed": True})
        res = runner.invoke(
            cli.app,
            [cmd, "-s", "snap-1", "--billing-project", "bp", "--billing-google-project-id", "gp"],
        )
        assert res.exit_code == 0, res.output
        assert captured["terra_billing_project"] == "bp"
        assert captured["billing_google_project_id"] == "gp"
        assert captured["config_path"] is None  # no config file unless --config is given


def test_billing_flags_default_to_none(monkeypatch):
    # Unset flags pass None so the pipeline falls back to config / built-in prod defaults.
    captured = _capture(monkeypatch, "run_tdr_qc", {"qc_passed": True})
    assert runner.invoke(cli.app, ["tdr-qc", "-s", "snap-1"]).exit_code == 0
    assert captured["terra_billing_project"] is None
    assert captured["billing_google_project_id"] is None


def test_ade_qc_azul_token_bucket_defaults_and_overrides(monkeypatch):
    from anvil_release_qc.azul_auth import DEFAULT_AZUL_TOKEN_BUCKET

    captured = _capture(monkeypatch, "run_ade_qc", {"qc_passed": True})
    assert runner.invoke(cli.app, ["ade-qc", "-s", "snap-1"]).exit_code == 0
    assert (
        captured["azul_token_bucket"]
        == DEFAULT_AZUL_TOKEN_BUCKET
        == ("fc-secure-886bea29-8c4e-4568-920a-a8cfee18e659")
    )
    res = runner.invoke(cli.app, ["ade-qc", "-s", "snap-1", "--azul-token-bucket", "my-bkt"])
    assert res.exit_code == 0
    assert captured["azul_token_bucket"] == "my-bkt"


def test_ade_qc_unusable_azul_token_exits_2(monkeypatch):
    # An unreadable / expired token is a "cannot run now" abort (exit 2), not a QC failure (exit 1).
    from anvil_release_qc.azul_auth import AzulTokenError

    def boom(**kwargs):
        raise AzulTokenError("Azul token expires in 60s (need > 1800s).")

    monkeypatch.setattr(cli, "run_ade_qc", boom)
    res = runner.invoke(cli.app, ["ade-qc", "-s", "snap-1"])
    assert res.exit_code == 2
