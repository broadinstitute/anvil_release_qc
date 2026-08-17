"""Tests for pipeline._create_workspace_resilient (createWorkspace timeout tolerance), no network.

Rawls createWorkspace can time out client-side even though the workspace was created server-side (seen
under batch QC at --max-concurrent 5). The helper turns that into a successful run by GET-ing the
workspace and adopting it, while still failing fast when the create genuinely didn't happen or the
existing workspace isn't the one we created.
"""

import types

import pytest
import requests

from anvil_data_qc import pipeline
from anvil_data_qc.pipeline import _create_workspace_resilient
from anvil_data_qc.safety import SafetyError


def _http_error(status: int) -> requests.exceptions.HTTPError:
    err = requests.exceptions.HTTPError(f"HTTP {status}")
    err.response = types.SimpleNamespace(status_code=status)
    return err


def _ws(domains):
    """A get_workspace response with the given auth-domain group names."""
    return {"workspace": {"authorizationDomain": [{"membersGroupName": d} for d in domains]}}


class FakeRawls:
    """Stand-in for RawlsClient: create_workspace raises a configured error; get_workspace replays
    a list of outcomes (each an exception to raise or a dict to return)."""

    def __init__(self, *, create_exc=None, get_results=None):
        self._create_exc = create_exc
        self._get_results = list(get_results or [])
        self.create_calls = 0
        self.get_calls = 0

    def create_workspace(self, namespace, name, *, description=""):
        self.create_calls += 1
        if self._create_exc is not None:
            raise self._create_exc
        return {"name": name}

    def get_workspace(self, namespace, name, fields=None):
        self.get_calls += 1
        result = self._get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)


def test_normal_path_no_adopt():
    rawls = FakeRawls()
    _create_workspace_resilient(rawls, "ns", "ws", description="d")
    assert rawls.create_calls == 1 and rawls.get_calls == 0  # created cleanly; no GET/adopt


def test_adopts_when_timeout_but_workspace_exists():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_ws([])],  # exists, empty auth domain (the import applies the real one)
    )
    _create_workspace_resilient(rawls, "ns", "ws", description="d")  # returns == adopted
    assert rawls.get_calls == 1


def test_adopts_after_transient_404():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ConnectionError("connection reset"),
        get_results=[_http_error(404), _ws([])],  # not visible yet, then appears
    )
    _create_workspace_resilient(rawls, "ns", "ws", description="d")
    assert rawls.get_calls == 2


def test_raises_when_workspace_never_created():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_http_error(404)] * pipeline.WORKSPACE_ADOPT_ATTEMPTS,
    )
    with pytest.raises(RuntimeError, match="was not created"):
        _create_workspace_resilient(rawls, "ns", "ws", description="d")
    assert rawls.get_calls == pipeline.WORKSPACE_ADOPT_ATTEMPTS


def test_refuses_to_adopt_workspace_with_auth_domain():
    # A workspace we just created must have an empty auth domain; a non-empty one isn't ours.
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[_ws(["AUTH_some_controlled_study"])],
    )
    with pytest.raises(SafetyError, match="non-empty auth domain"):
        _create_workspace_resilient(rawls, "ns", "ws", description="d")


def test_409_name_exists_fails_fast_not_adopted():
    # A 409 (name already exists) is not a timeout -> fail fast, never GET/adopt (unchanged behavior).
    rawls = FakeRawls(create_exc=_http_error(409))
    with pytest.raises(requests.exceptions.HTTPError):
        _create_workspace_resilient(rawls, "ns", "ws", description="d")
    assert rawls.get_calls == 0


def test_non_404_get_error_propagates():
    rawls = FakeRawls(
        create_exc=requests.exceptions.ReadTimeout("read timed out"),
        get_results=[
            _http_error(500)
        ],  # server error during the adopt-check -> propagate, don't mask
    )
    with pytest.raises(requests.exceptions.HTTPError):
        _create_workspace_resilient(rawls, "ns", "ws", description="d")
