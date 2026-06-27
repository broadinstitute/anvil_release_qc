"""Unit tests for client logic that doesn't need network (mocked HTTP)."""

import logging

import pytest

from anvil_data_qc.clients.azul import AzulClient
from anvil_data_qc.clients.base import BaseClient
from anvil_data_qc.clients.firecloud import FirecloudClient, import_job_id
from anvil_data_qc.clients.rawls import workspace_auth_domains
from anvil_data_qc.clients.tdr import (
    TdrClient,
    export_job_id,
    export_manifest_url,
    snapshot_bigquery_access,
    snapshot_name,
)
from anvil_data_qc.logging_setup import LOGGER_NAME
from anvil_data_qc.safety import BearerDestinationError, SignedUrl


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
    monkeypatch.setattr("anvil_data_qc.clients.azul.time.sleep", lambda _s: None)

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
    monkeypatch.setattr("anvil_data_qc.clients.azul.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.request_pfb_manifest("ds")


# --- Firecloud import job ---


def test_wait_for_import_succeeds(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    statuses = iter([{"status": "ReadyForUpsert"}, {"status": "Upserting"}, {"status": "Done"}])
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: next(statuses))
    monkeypatch.setattr("anvil_data_qc.clients.firecloud.time.sleep", lambda _s: None)
    job = client.wait_for_import("ns", "ws", "job-1", poll_interval=0)
    assert job["status"] == "Done"


def test_wait_for_import_raises_on_error(monkeypatch):
    client = FirecloudClient("https://fc.example", lambda: "token")
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: {"status": "Error"})
    monkeypatch.setattr("anvil_data_qc.clients.firecloud.time.sleep", lambda _s: None)
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


def test_tdr_export_snapshot_params(monkeypatch):
    client = TdrClient("https://tdr.example", lambda: "token")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("params")))
        return _Resp({"id": "job-1", "job_status": "running", "status_code": 202})

    monkeypatch.setattr(client, "request", fake_request)
    job = client.export_snapshot("snap-1")
    assert export_job_id(job) == "job-1"
    method, path, params = calls[0]
    assert method == "GET" and path == "/api/repository/v1/snapshots/snap-1/export"
    # Bools must be lowercase strings for TDR (not Python's "True"/"False").
    assert params == {"exportGsPaths": "false", "validatePrimaryKeyUniqueness": "true"}


def test_tdr_wait_for_export_job_succeeds(monkeypatch):
    client = TdrClient("https://tdr.example", lambda: "token")
    statuses = iter(
        [{"job_status": "running"}, {"job_status": "running"}, {"job_status": "succeeded"}]
    )
    monkeypatch.setattr(client, "get_job", lambda jid: next(statuses))
    monkeypatch.setattr("anvil_data_qc.clients.tdr.time.sleep", lambda _s: None)
    job = client.wait_for_export_job("job-1", poll_interval=0)
    assert job["job_status"] == "succeeded"


def test_tdr_wait_for_export_job_raises_on_failure(monkeypatch):
    client = TdrClient("https://tdr.example", lambda: "token")
    monkeypatch.setattr(client, "get_job", lambda jid: {"job_status": "failed"})
    monkeypatch.setattr("anvil_data_qc.clients.tdr.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.wait_for_export_job("job-1", poll_interval=0)


def test_export_job_id_missing_raises():
    with pytest.raises(ValueError):
        export_job_id({"job_status": "running"})


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
    assert snapshot_name(snap) == "ANVIL_CCDG_x"
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
