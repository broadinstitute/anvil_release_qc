"""Tests for pipeline._create_workspace_resilient (createWorkspace timeout tolerance), no network.

Rawls createWorkspace can time out client-side even though the workspace was created server-side (seen
under batch QC at --max-concurrent 5). The helper turns that into a successful run by GET-ing the
workspace and adopting it, while still failing fast when the create genuinely didn't happen or the
existing workspace isn't the one we created.
"""

import types

import pytest
import requests

from anvil_release_qc import pipeline
from anvil_release_qc.pipeline import _create_workspace_resilient
from anvil_release_qc.safety import SafetyError


def _http_error(status: int) -> requests.exceptions.HTTPError:
    err = requests.exceptions.HTTPError(f"HTTP {status}")
    err.response = types.SimpleNamespace(status_code=status)
    return err


def _ws(domains):
    """A get_workspace response with the given auth-domain group names."""
    return {"workspace": {"authorizationDomain": [{"membersGroupName": d} for d in domains]}}


class FakeRawls:
    """Stand-in for RawlsClient (create_workspace raises a configured error) plus the toolkit's
    TerraWorkspace (get_workspace_info replays a list of outcomes: an exception to raise or a dict)."""

    def __init__(self, *, create_exc=None, get_results=None):
        self._create_exc = create_exc
        self._get_results = list(get_results or [])
        self.create_calls = 0
        self.get_calls = 0
        self.request_util = types.SimpleNamespace(timeout=300)
        self.get_timeouts = []

    def create_workspace(self, namespace, name, *, description=""):
        self.create_calls += 1
        if self._create_exc is not None:
            raise self._create_exc
        return {"name": name}

    @property
    def workspace(self):
        return _FakeWorkspace(self)


class _FakeWorkspace:
    billing_project = "ns"
    workspace_name = "ws"

    def __init__(self, rawls):
        self._rawls = rawls
        self.request_util = rawls.request_util

    def get_workspace_info(self):
        self._rawls.get_calls += 1
        self._rawls.get_timeouts.append(self.request_util.timeout)
        result = self._rawls._get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(json=lambda: result)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)


def test_normal_path_no_adopt():
    rawls = FakeRawls()
    _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.create_calls == 1 and rawls.get_calls == 0  # created cleanly; no GET/adopt


def test_confirm_gets_use_short_timeout_then_restore():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("slow"), get_results=[_ws([])]
    )
    _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_timeouts == [pipeline.WORKSPACE_ADOPT_GET_TIMEOUT]
    assert rawls.request_util.timeout == 300  # restored for the later delete


def test_adopts_when_timeout_but_workspace_exists():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_ws([])],  # exists, empty auth domain (the import applies the real one)
    )
    _create_workspace_resilient(rawls, rawls.workspace, description="d")  # returns == adopted
    assert rawls.get_calls == 1


def test_adopts_after_transient_404():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ConnectionError("connection reset"),
        get_results=[_http_error(404), _ws([])],  # not visible yet, then appears
    )
    _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 2


def test_raises_when_workspace_never_created():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_http_error(404)] * pipeline.WORKSPACE_ADOPT_ATTEMPTS,
    )
    with pytest.raises(RuntimeError, match="could not be confirmed created"):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == pipeline.WORKSPACE_ADOPT_ATTEMPTS


def test_refuses_to_adopt_workspace_with_auth_domain():
    # A workspace we just created must have an empty auth domain; a non-empty one isn't ours.
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_ws(["AUTH_some_controlled_study"])],
    )
    with pytest.raises(SafetyError, match="non-empty auth domain"):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")


def test_409_name_exists_fails_fast_not_adopted():
    # A 409 (name already exists) is not a timeout -> fail fast, never GET/adopt (unchanged behavior).
    rawls = FakeRawls(create_exc=_http_error(409))
    with pytest.raises(requests.exceptions.HTTPError):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 0


def test_definitive_4xx_get_error_propagates():
    # A definitive 4xx (e.g. 403) during the adopt-check propagates -- don't mask it (5xx would retry).
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_http_error(403)],
    )
    with pytest.raises(requests.exceptions.HTTPError):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")


def test_retries_adopt_get_on_transient_error():
    # A transient error on the adopt GET (same load that timed out the create) is retried, then adopts.
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[requests.exceptions.ConnectionError("connection reset"), _ws([])],
    )
    _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 2


def test_non_timeout_create_error_fails_fast():
    # A create error that is neither a timeout nor a connection error is not caught -> fail fast, no GET.
    rawls = FakeRawls(create_exc=requests.exceptions.RequestException("unexpected"))
    with pytest.raises(requests.exceptions.RequestException):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 0


# --- gateway/5xx createWorkspace (the 502 seen in prod at cap-5) ---


def test_adopts_on_gateway_502():
    # A 502 Bad Gateway from createWorkspace (workspace created server-side) -> confirm-or-adopt.
    rawls = FakeRawls(create_exc=_http_error(502), get_results=[_ws([])])
    _create_workspace_resilient(rawls, rawls.workspace, description="d")  # returns == adopted
    assert rawls.get_calls == 1


def test_gateway_5xx_but_never_created_raises():
    rawls = FakeRawls(
        create_exc=_http_error(503),
        get_results=[_http_error(404)] * pipeline.WORKSPACE_ADOPT_ATTEMPTS,
    )
    with pytest.raises(RuntimeError, match="could not be confirmed created"):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")


def test_4xx_create_error_fails_fast_not_adopted():
    # A definitive 4xx from create (e.g. 403 no-access) is NOT a gateway error -> fail fast, no GET/adopt.
    rawls = FakeRawls(create_exc=_http_error(403))
    with pytest.raises(requests.exceptions.HTTPError):
        _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 0


def test_retries_adopt_get_on_transient_5xx():
    # A transient 5xx on the adopt GET (same gateway pressure) is retried, then adopts.
    rawls = FakeRawls(
        create_exc=_http_error(502),
        get_results=[_http_error(502), _ws([])],
    )
    _create_workspace_resilient(rawls, rawls.workspace, description="d")
    assert rawls.get_calls == 2


# --- cleanup of the QC workspace after a passing QC ---


class _DeletableWorkspace:
    billing_project = "ns"
    workspace_name = "ws"

    def __init__(self, exc=None):
        self._exc = exc
        self.deleted = 0

    def delete_workspace(self):
        self.deleted += 1
        if self._exc is not None:
            raise self._exc


def test_delete_workspace_deletes_it():
    ws = _DeletableWorkspace()
    pipeline._delete_workspace(ws)
    assert ws.deleted == 1


def test_delete_workspace_failure_is_swallowed_and_logged(caplog):
    ws = _DeletableWorkspace(exc=_http_error(500))
    pipeline._delete_workspace(ws)  # must not raise: a failed cleanup never changes the QC result
    assert ws.deleted == 1
    assert "left in place" in caplog.text
