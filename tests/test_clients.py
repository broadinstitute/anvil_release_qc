"""Unit tests for client logic that doesn't need network (mocked HTTP)."""

import pytest

from anvil_data_qc.clients.azul import AzulClient
from anvil_data_qc.clients.base import BaseClient
from anvil_data_qc.clients.firecloud import FirecloudClient, import_job_id
from anvil_data_qc.clients.rawls import workspace_auth_domains
from anvil_data_qc.clients.tdr import snapshot_bigquery_access, snapshot_name
from anvil_data_qc.safety import BearerDestinationError


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
