"""Unit tests for client logic that doesn't need network (mocked HTTP)."""

import pytest

from anvil_data_qc.clients.azul import AzulClient
from anvil_data_qc.clients.firecloud import FirecloudClient, import_job_id
from anvil_data_qc.clients.rawls import workspace_auth_domains
from anvil_data_qc.clients.tdr import snapshot_bigquery_access, snapshot_name


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- Azul manifest poll ---

def test_azul_request_pfb_manifest_polls_to_signed_url(monkeypatch):
    client = AzulClient("https://azul.example", credentials=None, catalog="anvil14")
    responses = iter(
        [
            _Resp({"Status": 301, "Location": "https://azul.example/fetch/manifest/files/t1", "Retry-After": 0}),
            _Resp({"Status": 301, "Location": "https://azul.example/fetch/manifest/files/t2", "Retry-After": 0}),
            _Resp({"Status": 302, "Location": "https://s3.example/m.avro?X-Amz-Signature=zzz", "Retry-After": 0}),
        ]
    )
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr("anvil_data_qc.clients.azul.time.sleep", lambda _s: None)

    url = client.request_pfb_manifest("265ecd43-c88f-19e9-4da1-b6cd1d07a557")
    assert url == "https://s3.example/m.avro?X-Amz-Signature=zzz"
    # First call is the PUT with the dataset_id filter and verbatim.pfb format.
    method, path, params = calls[0]
    assert method == "PUT" and path == "/fetch/manifest/files"
    assert "265ecd43-c88f-19e9-4da1-b6cd1d07a557" in params["filters"]
    assert '"datasets.dataset_id"' in params["filters"]
    assert params["format"] == "verbatim.pfb"
    assert params["catalog"] == "anvil14"


def test_azul_poll_raises_on_unexpected_status(monkeypatch):
    client = AzulClient("https://azul.example", credentials=None, catalog="anvil14")
    monkeypatch.setattr(client, "request", lambda *a, **k: _Resp({"Status": 500}))
    monkeypatch.setattr("anvil_data_qc.clients.azul.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.request_pfb_manifest("ds")


# --- Firecloud import job ---

def test_wait_for_import_succeeds(monkeypatch):
    client = FirecloudClient("https://fc.example", credentials=None)
    statuses = iter([{"status": "Running"}, {"status": "Done"}])
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: next(statuses))
    monkeypatch.setattr("anvil_data_qc.clients.firecloud.time.sleep", lambda _s: None)
    job = client.wait_for_import("ns", "ws", "job-1", poll_interval=0)
    assert job["status"] == "Done"


def test_wait_for_import_raises_on_error(monkeypatch):
    client = FirecloudClient("https://fc.example", credentials=None)
    monkeypatch.setattr(client, "get_import_job", lambda ns, n, jid: {"status": "Error"})
    monkeypatch.setattr("anvil_data_qc.clients.firecloud.time.sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        client.wait_for_import("ns", "ws", "job-1", poll_interval=0)


def test_import_job_id():
    assert import_job_id({"jobId": "abc"}) == "abc"
    assert import_job_id({"id": "xyz"}) == "xyz"
    with pytest.raises(ValueError):
        import_job_id({})


# --- TDR + Rawls extraction helpers ---

def test_tdr_snapshot_extraction():
    snap = {
        "name": "ANVIL_CCDG_x",
        "accessInformation": {"bigQuery": {"projectId": "datarepo-183d3f73", "datasetName": "anvil_x"}},
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
