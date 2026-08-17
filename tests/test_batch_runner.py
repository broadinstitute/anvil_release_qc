"""Tests for qc_batch_runner (anvil_data_qc.batch), no network — a FakeClient stands in for Terra."""

import types

import pytest
import requests

from anvil_data_qc.batch import runner
from anvil_data_qc.batch.cli import _read_snapshot_ids
from anvil_data_qc.batch.runner import (
    MAX_CONCURRENT_CEILING,
    BatchParams,
    Outcome,
    RunnerFatalError,
    batch_drain_policy,
    check_azul_token,
    classify,
    ensure_token_available,
    resolve_method_config,
    run_batch,
    sliding_window_policy,
)
from anvil_data_qc.config import ResolvedTier


def _tier():
    return ResolvedTier(
        name="prod",
        email="op@broadinstitute.org",
        adc_credentials_file=None,
        terra_billing_project="anvil-developer-work",
        billing_google_project_id="terra-xxxx",
        qc_workspace_name="anvil-data-qc-shared",
        azul_token_file=None,
        service_urls={"firecloud": "https://fc.example/"},
    )


class _Http404(requests.HTTPError):
    def __init__(self):
        super().__init__("404 Not Found")
        self.response = types.SimpleNamespace(status_code=404)


class FakeClient:
    """Stands in for TerraWorkspaceClient. Configurable configs/entities/statuses; tracks concurrency."""

    def __init__(
        self,
        *,
        method_configs=None,
        entities=None,
        ws_attrs=None,
        workflow_status=None,  # sid -> "Succeeded"|"Failed"|"Aborted" (default Succeeded)
        post_run=None,  # sid -> attrs the "workflow" writes into the row when its submission goes terminal
        fail_launch=(),  # sids whose create_submission raises
        poll_fail_once=(),  # sids whose FIRST get_submission raises (then terminal)
    ):
        self.namespace, self.name = "anvil-developer-work", "anvil-data-qc-shared"
        self._configs = list(method_configs or [])
        self._entities = dict(entities or {})
        self._ws_attrs = dict(ws_attrs or {})
        self._wf = dict(workflow_status or {})
        self._post_run = dict(post_run or {})
        self._fail_launch = set(fail_launch)
        self._poll_fail_once = set(poll_fail_once)
        self.created = []
        self._live = set()
        self.max_live = 0
        self._sub_sid = {}
        self._polled = set()
        self._n = 0

    def find_method_config(self, name):
        return next((c for c in self._configs if c.get("name") == name), None)

    def get_entity(self, entity_type, name):
        if name not in self._entities:
            raise _Http404()
        return {"name": name, "attributes": self._entities[name]}

    def get_workspace_attributes(self):
        return dict(self._ws_attrs)

    def create_submission(
        self,
        *,
        method_config_namespace,
        method_config_name,
        entity_type,
        entity_name,
        use_call_cache=False,
        user_comment=None,
    ):
        assert use_call_cache is False  # runner must always disable call caching
        if entity_name in self._fail_launch:
            raise RuntimeError("simulated launch failure")
        self._n += 1
        sub_id = f"sub-{self._n}"
        self._sub_sid[sub_id] = entity_name
        self.created.append(entity_name)
        self._live.add(sub_id)
        self.max_live = max(self.max_live, len(self._live))
        return {"submissionId": sub_id}

    def get_submission(self, sub_id):
        sid = self._sub_sid[sub_id]
        if sid in self._poll_fail_once and sub_id not in self._polled:
            self._polled.add(sub_id)
            raise RuntimeError("simulated poll error")
        if sub_id in self._live:  # transition to terminal: the workflow "writes" its outputs into the row
            self._live.discard(sub_id)
            if sid in self._post_run:
                self._entities[sid] = {**self._entities.get(sid, {}), **self._post_run[sid]}
        return {"status": "Done", "workflows": [{"status": self._wf.get(sid, "Succeeded")}]}


# --- TSV parsing (cli._read_snapshot_ids) ---


def test_read_snapshot_ids_ok(tmp_path):
    p = tmp_path / "s.tsv"
    p.write_text("snapshot_id\ta\nx1\tfoo\nx2\tbar\nx1\tdup\n\t\n")
    assert _read_snapshot_ids(p) == ["x1", "x2"]  # order preserved, deduped, blanks dropped


def test_read_snapshot_ids_missing_column(tmp_path):
    p = tmp_path / "s.tsv"
    p.write_text("id\tname\nx1\tfoo\n")
    with pytest.raises(RunnerFatalError, match="snapshot_id"):
        _read_snapshot_ids(p)


def test_read_snapshot_ids_empty(tmp_path):
    p = tmp_path / "s.tsv"
    p.write_text("snapshot_id\n\n")
    with pytest.raises(RunnerFatalError):
        _read_snapshot_ids(p)


# --- resolve_method_config ---


def test_resolve_method_config_ok():
    client = FakeClient(
        method_configs=[
            {
                "name": "anvil-data-qc-ade-qc",
                "namespace": "anvil-developer-work",
                "rootEntityType": "anvil15_qc",
            }
        ]
    )
    assert resolve_method_config(client, "ade_qc", "anvil15_qc") == (
        "anvil-developer-work",
        "anvil-data-qc-ade-qc",
    )


def test_resolve_method_config_not_found():
    with pytest.raises(RunnerFatalError, match="not found"):
        resolve_method_config(FakeClient(method_configs=[]), "ade_qc", "anvil15_qc")


def test_resolve_method_config_root_entity_mismatch():
    client = FakeClient(
        method_configs=[
            {"name": "anvil-data-qc-ade-qc", "namespace": "x", "rootEntityType": "anvil14_qc"}
        ]
    )
    with pytest.raises(RunnerFatalError, match="rootEntityType"):
        resolve_method_config(client, "ade_qc", "anvil15_qc")


# --- classify (status -> outcome) ---


def _submission(wf_status="Succeeded"):
    return {"status": "Done", "workflows": [{"status": wf_status}]}


def test_classify_passed_by_exit_code():
    client = FakeClient(entities={"s": {"ade_qc_exit_code": 0}})
    assert classify(client, "anvil15_qc", "s", "ade_qc", _submission()) is Outcome.PASSED


def test_classify_passed_by_flag():
    client = FakeClient(entities={"s": {"ade_qc_passed": True}})
    assert classify(client, "anvil15_qc", "s", "ade_qc", _submission()) is Outcome.PASSED


def test_classify_qc_failed_exit_1():
    client = FakeClient(entities={"s": {"ade_qc_passed": False, "ade_qc_exit_code": 1}})
    assert classify(client, "anvil15_qc", "s", "ade_qc", _submission()) is Outcome.QC_FAILED


def test_classify_safety_abort_exit_2():
    client = FakeClient(entities={"s": {"ade_qc_exit_code": 2}})
    assert classify(client, "anvil15_qc", "s", "ade_qc", _submission()) is Outcome.SAFETY_ABORT


def test_classify_infra_failed_workflow_failed():
    client = FakeClient(entities={"s": {}})  # empty columns
    assert (
        classify(client, "anvil15_qc", "s", "ade_qc", _submission("Failed")) is Outcome.INFRA_FAILED
    )


def test_classify_incomplete_when_succeeded_but_no_verdict():
    client = FakeClient(entities={"s": {}})
    assert (
        classify(client, "anvil15_qc", "s", "ade_qc", _submission("Succeeded"))
        is Outcome.INCOMPLETE
    )


# --- run_batch: batch-drain, concurrency cap, idempotency, continue-on-failure ---


def _cfg(name="anvil-data-qc-tdr-qc"):
    return [{"name": name, "namespace": "anvil-developer-work", "rootEntityType": "anvil15_qc"}]


def _run(client, params, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    return run_batch(client=client, creds=object(), tier=_tier(), params=params)


def test_run_batch_tdr_all_pass_never_exceeds_cap(monkeypatch):
    ids = [f"s{i}" for i in range(12)]
    # Rows start empty (not yet run) so all are eligible; each workflow "writes" exit 0 on completion.
    client = FakeClient(
        method_configs=_cfg(),
        entities={i: {} for i in ids},
        post_run={i: {"tdr_qc_exit_code": 0} for i in ids},
    )
    params = BatchParams(
        pipeline="tdr_qc", entity_type="anvil15_qc", snapshot_ids=ids, max_concurrent=5
    )
    result = _run(client, params, monkeypatch)
    assert client.created == ids  # all launched
    assert client.max_live <= 5  # never exceeded the cap
    assert all(o is Outcome.PASSED for o in result.outcomes.values())
    assert result.exit_code == 0


def test_run_batch_idempotency_skips_already_passed(monkeypatch):
    ids = ["a", "b", "c"]
    # a/b already passed (flag / exit 0); c not yet run (empty row) -> runs, then the workflow writes exit 0.
    client = FakeClient(
        method_configs=_cfg(),
        entities={"a": {"tdr_qc_passed": True}, "b": {"tdr_qc_exit_code": 0}, "c": {}},
        post_run={"c": {"tdr_qc_exit_code": 0}},
    )
    params = BatchParams(
        pipeline="tdr_qc", entity_type="anvil15_qc", snapshot_ids=ids, max_concurrent=5
    )
    result = _run(client, params, monkeypatch)
    assert client.created == ["c"]  # a,b skipped (already passed); only c launched
    assert result.outcomes["a"] is Outcome.SKIPPED and result.outcomes["b"] is Outcome.SKIPPED
    assert result.outcomes["c"] is Outcome.PASSED


def test_run_batch_force_reruns_passed(monkeypatch):
    client = FakeClient(method_configs=_cfg(), entities={"a": {"tdr_qc_passed": True}})
    params = BatchParams(
        pipeline="tdr_qc",
        entity_type="anvil15_qc",
        snapshot_ids=["a"],
        max_concurrent=5,
        force=True,
    )
    _run(client, params, monkeypatch)
    assert client.created == ["a"]  # --force re-runs it


def test_run_batch_missing_row_is_fatal(monkeypatch):
    client = FakeClient(method_configs=_cfg(), entities={"a": {}})
    params = BatchParams(
        pipeline="tdr_qc",
        entity_type="anvil15_qc",
        snapshot_ids=["a", "ghost"],
        max_concurrent=5,
        force=True,
    )
    with pytest.raises(RunnerFatalError, match="not rows"):
        _run(client, params, monkeypatch)


def test_run_batch_continue_on_failure(monkeypatch):
    ids = ["ok", "boom", "qcfail"]
    client = FakeClient(
        method_configs=_cfg(),
        entities={
            "ok": {"tdr_qc_exit_code": 0},
            "boom": {"tdr_qc_exit_code": 0},
            "qcfail": {"tdr_qc_exit_code": 1},
        },
        fail_launch=["boom"],
    )
    params = BatchParams(
        pipeline="tdr_qc", entity_type="anvil15_qc", snapshot_ids=ids, max_concurrent=5, force=True
    )
    result = _run(client, params, monkeypatch)
    assert result.outcomes["ok"] is Outcome.PASSED
    assert result.outcomes["boom"] is Outcome.LAUNCH_ERROR  # recorded, batch continued
    assert result.outcomes["qcfail"] is Outcome.QC_FAILED
    assert result.exit_code == 1  # at least one failure


def test_run_batch_poll_error_does_not_over_launch(monkeypatch):
    ids = [f"s{i}" for i in range(8)]
    client = FakeClient(
        method_configs=_cfg(),
        entities={i: {"tdr_qc_exit_code": 0} for i in ids},
        poll_fail_once=[
            "s2"
        ],  # one straggler's first poll errors -> stays in-flight one extra cycle
    )
    params = BatchParams(
        pipeline="tdr_qc", entity_type="anvil15_qc", snapshot_ids=ids, max_concurrent=5, force=True
    )
    result = _run(client, params, monkeypatch)
    assert client.max_live <= 5  # never over-launched while the straggler was still in-flight
    assert all(o is Outcome.PASSED for o in result.outcomes.values())


def test_run_batch_max_concurrent_out_of_range(monkeypatch):
    client = FakeClient(method_configs=_cfg(), entities={"a": {}})
    params = BatchParams(
        pipeline="tdr_qc",
        entity_type="anvil15_qc",
        snapshot_ids=["a"],
        max_concurrent=MAX_CONCURRENT_CEILING + 1,
    )
    with pytest.raises(RunnerFatalError, match="out of range"):
        _run(client, params, monkeypatch)


def test_run_batch_dry_run_submits_nothing(monkeypatch):
    ids = ["a", "b"]
    client = FakeClient(method_configs=_cfg(), entities={"a": {}, "b": {"tdr_qc_passed": True}})
    params = BatchParams(
        pipeline="tdr_qc",
        entity_type="anvil15_qc",
        snapshot_ids=ids,
        max_concurrent=5,
        dry_run=True,
    )
    result = _run(client, params, monkeypatch)
    assert client.created == []  # no submissions in dry-run
    assert result.outcomes["a"] is Outcome.WOULD_RUN and result.outcomes["b"] is Outcome.SKIPPED
    assert result.exit_code == 0


# --- policies ---


def test_launch_policies():
    assert batch_drain_policy(0, 5) is True and batch_drain_policy(1, 5) is False
    assert sliding_window_policy(4, 5) is True and sliding_window_policy(5, 5) is False


# --- Azul token gate ---


def test_check_azul_token_not_set():
    client = FakeClient(ws_attrs={})
    ok, detail = check_azul_token(client, object(), _tier(), 2100)
    assert ok is False and "not set" in detail


def test_check_azul_token_ok(monkeypatch):
    client = FakeClient(ws_attrs={"anvil_data_qc_azul_token": "gs://b/tok.txt"})
    monkeypatch.setattr(runner, "read_gcs_text", lambda creds, uri: "the-token")
    monkeypatch.setattr(runner, "expected_client_id", lambda tier: "cid")
    monkeypatch.setattr(
        runner, "validate_azul_token", lambda *a, **k: {"email": "op@x.org", "expires_in": 3000}
    )
    ok, detail = check_azul_token(client, object(), _tier(), 2100)
    assert ok is True and "op@x.org" in detail


def test_check_azul_token_fails_validation(monkeypatch):
    client = FakeClient(ws_attrs={"anvil_data_qc_azul_token": "gs://b/tok.txt"})
    monkeypatch.setattr(runner, "read_gcs_text", lambda creds, uri: "the-token")
    monkeypatch.setattr(runner, "expected_client_id", lambda tier: "cid")

    def _boom(*a, **k):
        raise runner.AzulTokenError("expires in 100s (need > 2100s)")

    monkeypatch.setattr(runner, "validate_azul_token", _boom)
    ok, detail = check_azul_token(client, object(), _tier(), 2100)
    assert ok is False and "2100" in detail


def test_ensure_token_wait_times_out(monkeypatch):
    client = FakeClient(ws_attrs={})  # token never available
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    params = BatchParams(
        pipeline="ade_qc",
        entity_type="anvil15_qc",
        snapshot_ids=["a"],
        token_poll_seconds=1,
        token_max_wait_seconds=3,
    )
    with pytest.raises(RunnerFatalError, match="No usable Azul token"):
        ensure_token_available(client, object(), _tier(), params, interactive=False)


def test_ensure_token_prompt_then_ok(monkeypatch):
    client = FakeClient(ws_attrs={"anvil_data_qc_azul_token": "gs://b/tok.txt"})
    states = iter([False, True])
    monkeypatch.setattr(runner, "check_azul_token", lambda *a, **k: (next(states), "detail"))
    monkeypatch.setattr(
        "builtins.input", lambda *a, **k: ""
    )  # operator "refreshes" then presses Enter
    params = BatchParams(pipeline="ade_qc", entity_type="anvil15_qc", snapshot_ids=["a"])
    ensure_token_available(
        client, object(), _tier(), params, interactive=True
    )  # returns without raising
