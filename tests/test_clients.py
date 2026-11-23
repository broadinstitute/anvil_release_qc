"""Unit tests for client logic that doesn't need network (mocked HTTP)."""

import logging

import pytest
import requests

from anvil_release_qc.clients.azul import AzulClient
from anvil_release_qc.clients.base import BaseClient
from anvil_release_qc.clients.firecloud import FirecloudClient, import_job_id
from anvil_release_qc.clients.rawls import workspace_auth_domains
from anvil_release_qc.clients.tdr import (
    export_manifest_url,
    snapshot_bigquery_access,
    wait_for_export_job,
)
from anvil_release_qc.clients.toolkit import GuardedRunRequest
from anvil_release_qc.logging_setup import LOGGER_NAME
from anvil_release_qc.safety import BearerDestinationError, SignedUrl


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- Azul manifest poll ---


def test_azul_request_pfb_manifest_polls_to_signed_url(monkeypatch):
    client = AzulClient("https://azul.example", lambda: "token", catalog="anvil14")
    responses = iter(
        [
            _Resp(
                {
                    "Status": 301,
                    "Location": "https://azul.example/fetch/manifest/files/t1",
                    "Retry-After": 0,
                }
            ),
            _Resp(
                {
                    "Status": 301,
                    "Location": "https://azul.example/fetch/manifest/files/t2",
                    "Retry-After": 0,
                }
            ),
            _Resp(
                {
                    "Status": 302,
                    "Location": "https://s3.example/m.avro?X-Amz-Signature=zzz",
                    "Retry-After": 0,
                }
            ),
        ]
    )
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr("anvil_release_qc.clients.azul.time.sleep", lambda _s: None)

    signed = client.request_pfb_manifest("265ecd43-c88f-19e9-4da1-b6cd1d07a557")
    assert signed.reveal() == "https://s3.example/m.avro?X-Amz-Signature=zzz"  # SignedUrl wrapper
    assert signed.location == "https://s3.example/m.avro"  # query (secret) dropped
    # First call is the PUT with the dataset_id filter and verbatim.pfb format.
    method, path, params = calls[0]
    assert method == "PUT" and path == "/fetch/manifest/files"
    assert "265ecd43-c88f-19e9-4da1-b6cd1d07a557" in params["filters"]
    assert '"datasets.dataset_id"' in params["filters"]
    assert params["format"] == "verbatim.pfb"
    assert params["catalog"] == "anvil14"


def test_azul_poll_raises_on_unexpected_status(monkeypatch):
    client = AzulClient("https://azul.example", lambda: "token", catalog="anvil14")
    monkeypatch.setattr(client, "request", lambda *a, **k: _Resp({"Status": 500}))
    monkeypatch.setattr("anvil_release_qc.clients.azul.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.request_pfb_manifest("ds")


# --- Firecloud import job ---


def test_wait_for_import_succeeds(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    statuses = iter([{"status": "ReadyForUpsert"}, {"status": "Upserting"}, {"status": "Done"}])
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: next(statuses))
    monkeypatch.setattr("anvil_release_qc.clients.firecloud.time.sleep", lambda _s: None)
    job = client.wait_for_import("ns", "ws", "job-1", poll_interval=0)
    assert job["status"] == "Done"


def test_wait_for_import_raises_on_error(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: {"status": "Error"})
    monkeypatch.setattr("anvil_release_qc.clients.firecloud.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.wait_for_import("ns", "ws", "job-1", poll_interval=0)


def test_import_job_id():
    assert import_job_id({"jobId": "abc"}) == "abc"
    assert import_job_id({"id": "xyz"}) == "xyz"
    with pytest.raises(ValueError):
        import_job_id({})


def test_submit_import_job_defaults_to_pfb(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured["path"] = path
        captured["body"] = kwargs.get("json_body")
        return _Resp({"jobId": "j1"})

    monkeypatch.setattr(client, "request", fake_request)
    client.submit_import_job("ns", "ws", SignedUrl("https://s3.example/m.avro?X-Amz-Signature=x"))
    assert captured["path"] == "/api/workspaces/ns/ws/importJob"
    # ade-qc default: pfb / None; reveal() happens at this single site.
    assert captured["body"] == {
        "url": "https://s3.example/m.avro?X-Amz-Signature=x",
        "filetype": "pfb",
        "options": None,
    }


def test_submit_import_job_tdrexport_options(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    captured = {}

    def fake_request(method, path, **kwargs):
        captured["body"] = kwargs.get("json_body")
        return _Resp({"jobId": "j2"})

    monkeypatch.setattr(client, "request", fake_request)
    client.submit_import_job(
        "ns",
        "ws",
        SignedUrl("https://storage.googleapis.com/b/manifest.json?X-Goog-Signature=x"),
        filetype="tdrexport",
        options={"tdrSyncPermissions": False},
    )
    assert captured["body"]["filetype"] == "tdrexport"
    assert captured["body"]["options"] == {"tdrSyncPermissions": False}
    assert (
        captured["body"]["url"]
        == "https://storage.googleapis.com/b/manifest.json?X-Goog-Signature=x"
    )


# --- TDR snapshot export (tdr-qc) ---


class _FakeTdr:
    """Stand-in for the toolkit's TDR: replays get_job_status payloads."""

    def __init__(self, statuses=()):
        self._statuses = iter(statuses)

    def get_job_status(self, job_id):
        return _Resp(next(self._statuses))


def test_tdr_wait_for_export_job_succeeds(monkeypatch):
    tdr = _FakeTdr(
        [{"job_status": "running"}, {"job_status": "running"}, {"job_status": "succeeded"}]
    )
    monkeypatch.setattr("anvil_release_qc.clients.tdr.time.sleep", lambda _s: None)
    job = wait_for_export_job(tdr, "job-1", poll_interval=0)
    assert job["job_status"] == "succeeded"


def test_tdr_wait_for_export_job_raises_on_failure(monkeypatch):
    tdr = _FakeTdr([{"job_status": "failed", "status_code": 500}])
    monkeypatch.setattr("anvil_release_qc.clients.tdr.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError) as excinfo:
        wait_for_export_job(tdr, "job-1", poll_interval=0)
    # The error names the job + status but does NOT dump the whole payload (redaction discipline).
    assert "job-1" in str(excinfo.value) and "failed" in str(excinfo.value)


def test_tdr_wait_for_export_job_polls_through_unknown_status(monkeypatch):
    # An unknown OR missing job_status is treated as still-in-progress (keep polling), not terminal.
    tdr = _FakeTdr(
        [
            {"job_status": "queued"},  # unknown (not running/succeeded/failed)
            {},  # missing job_status entirely
            {"job_status": "running"},
            {"job_status": "succeeded"},
        ]
    )
    monkeypatch.setattr("anvil_release_qc.clients.tdr.time.sleep", lambda _s: None)
    job = wait_for_export_job(tdr, "job-1", poll_interval=0)
    assert job["job_status"] == "succeeded"  # polled through queued / missing / running


def test_tdr_wait_for_export_job_times_out(monkeypatch):
    # A job that never reaches a terminal state raises TimeoutError once the deadline passes.
    tdr = _FakeTdr([{"job_status": "running"}] * 5)
    monkeypatch.setattr("anvil_release_qc.clients.tdr.time.sleep", lambda _s: None)
    # deadline = monotonic() + max_wait = 100.0; the first in-loop check (200.0) is past it.
    ticks = iter([100.0, 200.0])
    monkeypatch.setattr("anvil_release_qc.clients.tdr.time.monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        wait_for_export_job(tdr, "job-1", poll_interval=0, max_wait=0)

def test_export_manifest_url_extracts_signed_manifest():
    bucket = "https://storage.googleapis.com/datarepo-abc-snapshot-export-bucket/job1"
    result = {
        "format": {
            "parquet": {
                "manifest": f"{bucket}/manifest.json?X-Goog-Signature=zzz",
                # per-table parquet paths are present but NOT what the import consumes:
                "location": {
                    "tables": [
                        {
                            "name": "sample",
                            "paths": [f"{bucket}/sample/s.parquet?X-Goog-Signature=q"],
                        }
                    ]
                },
            }
        }
    }
    signed = export_manifest_url(result)
    assert (
        signed.reveal() == f"{bucket}/manifest.json?X-Goog-Signature=zzz"
    )  # the manifest, wrapped
    assert (
        signed.location == f"{bucket}/manifest.json"
    )  # signed query dropped from the safe-to-log form


def test_export_manifest_url_missing_raises():
    with pytest.raises(ValueError):
        export_manifest_url({"format": {"parquet": {"location": {"tables": []}}}})  # no manifest
    with pytest.raises(ValueError):
        export_manifest_url({})


# --- TDR + Rawls extraction helpers ---


def test_tdr_snapshot_extraction():
    snap = {
        "name": "ANVIL_CCDG_x",
        "accessInformation": {
            "bigQuery": {"projectId": "datarepo-183d3f73", "datasetName": "anvil_x"}
        },
    }
    assert snapshot_bigquery_access(snap) == ("datarepo-183d3f73", "anvil_x")


def test_tdr_missing_bq_access_raises():
    with pytest.raises(ValueError):
        snapshot_bigquery_access({"name": "x"})


def test_workspace_auth_domains():
    ws = {"workspace": {"authorizationDomain": [{"membersGroupName": "AUTH_ANVIL_x"}]}}
    assert workspace_auth_domains(ws) == ["AUTH_ANVIL_x"]
    assert workspace_auth_domains({"workspace": {}}) == []


# --- base client: bearer-token destination pinning (a token goes only to its own HTTPS host) ---


class _Http200:
    status_code = 200
    reason = "OK"
    headers: dict = {}

    def raise_for_status(self):
        pass


def _pinned_client(base_url="https://good.example", token="tok"):
    client = BaseClient(base_url, lambda: token)
    client._session.request = lambda *a, **k: _Http200()  # no network
    return client


def test_bearer_refused_to_other_host():
    # An off-host absolute URL while carrying the token (e.g. an unexpected Azul poll Location).
    with pytest.raises(BearerDestinationError):
        _pinned_client().request("GET", "https://evil.example/fetch/manifest/files/tok")


def test_bearer_refused_over_http():
    with pytest.raises(BearerDestinationError):
        _pinned_client().request("GET", "http://good.example/x")  # right host, but not HTTPS


def test_bearer_allowed_to_own_https_host():
    client = _pinned_client()
    client.request("GET", "/relative/path")  # -> own host
    client.request("GET", "https://good.example/absolute")  # same host (no raise)


def test_anonymous_request_not_host_pinned():
    # No bearer -> nothing to leak, so the host pin does not apply.
    _pinned_client(token="").request("GET", "https://other.example/x")  # no raise


def test_bearer_refusal_is_logged(caplog):
    # The refusal is audit-logged (host only, no token) before raising.
    client = _pinned_client(token="SECRETxyz123")
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(BearerDestinationError):
            client.request("GET", "https://evil.example/x")
    assert "Refused to send a bearer token" in caplog.text
    assert "SECRETxyz123" not in caplog.text  # the token value is never in the audit log


# --- toolkit adapters (GuardedRunRequest): the same pinning/redaction guarantees as BaseClient ---


class _Http200Resp(_Http200):
    text = ""


def _guarded(monkeypatch, token="tok", base="https://good.example"):
    seen = {}

    def fake_request(method, uri, **kwargs):
        seen.update(method=method, uri=uri, **kwargs)
        return _Http200Resp()

    monkeypatch.setattr("anvil_release_qc.clients.toolkit.requests.request", fake_request)
    return GuardedRunRequest(lambda: token, base), seen


def test_guarded_run_request_sends_token_to_pinned_host(monkeypatch):
    rr, seen = _guarded(monkeypatch)
    rr.run_request("https://good.example/api/x", "GET")
    assert seen["headers"]["Authorization"] == "Bearer tok" and seen["timeout"] == 60


def test_guarded_run_request_refuses_other_host_and_http(monkeypatch):
    rr, seen = _guarded(monkeypatch)
    with pytest.raises(BearerDestinationError):
        rr.run_request("https://evil.example/x", "GET")
    with pytest.raises(BearerDestinationError):
        rr.run_request("http://good.example/x", "GET")
    assert not seen  # nothing was sent


def test_guarded_run_request_refusal_does_not_log_token(monkeypatch, caplog):
    rr, _ = _guarded(monkeypatch, token="SECRETxyz123")
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(BearerDestinationError):
            rr.run_request("https://evil.example/x", "GET")
    assert "SECRETxyz123" not in caplog.text


def test_guarded_run_request_http_error_not_retried(monkeypatch):
    class _Err403(_Http200Resp):
        status_code = 403

        def raise_for_status(self):
            raise requests.HTTPError("403")

    calls = []
    monkeypatch.setattr(
        "anvil_release_qc.clients.toolkit.requests.request",
        lambda *a, **k: calls.append(1) or _Err403(),
    )
    rr = GuardedRunRequest(lambda: "t", "https://good.example")
    with pytest.raises(requests.HTTPError):
        rr.run_request("https://good.example/x", "GET")
    assert len(calls) == 1  # no backoff/retry
    rr.run_request("https://good.example/x", "GET", accept_return_codes=[403])  # tolerated code


def test_make_tdr_and_workspace_hosts_match_tier_config():
    from anvil_release_qc.clients.toolkit import make_tdr, make_terra_workspace
    from anvil_release_qc.config import SERVICE_URLS, ResolvedTier

    for name in SERVICE_URLS:
        tier = ResolvedTier(
            name=name,
            adc_credentials_file=None,
            azul_token_file=None,
            terra_billing_project="bp",
            billing_google_project_id="gp",
            service_urls=SERVICE_URLS[name],
        )
        make_tdr(tier, lambda: "t")  # raises if the toolkit's TDR host drifted from config
        make_terra_workspace(tier, lambda: "t", "ns", "ws")
