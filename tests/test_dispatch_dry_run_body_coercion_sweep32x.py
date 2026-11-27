"""Hermetic contract tests for /api/outbox/dispatch dry_run body coercion.

Only an explicit JSON boolean may override the query-parameter default. A null,
number, empty string or other non-boolean value must not silently escalate a
preview (dry_run=True) into a live outbox dispatch (dry_run=False).
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auto_assign.main import app


class RecordingService:
    """Minimal in-process service that records the resolved dry_run argument."""

    def __init__(self):
        self.settings = SimpleNamespace(
            api_token="sweep32x-token",
            allow_unauthenticated_local_dev=False,
        )
        self.seen = None

    async def dispatch_outbox(self, dry_run, limit):
        self.seen = {"dry_run": dry_run, "limit": limit}
        return {"dry_run": dry_run, "limit": limit}


def _post(body, query=""):
    svc = RecordingService()
    app.state.assignment_service = svc
    client = TestClient(app)
    response = client.post(
        f"/api/outbox/dispatch{query}",
        headers={"Authorization": "Bearer sweep32x-token"},
        json=body,
    )
    assert response.status_code == 200, response.text
    return svc.seen


@pytest.mark.parametrize(
    "value",
    [None, 0, 1, "", "false", [], {}],
    ids=["null", "zero", "one", "empty-string", "string-false", "empty-list", "empty-dict"],
)
def test_non_boolean_dry_run_body_keeps_preview_default(value):
    """A non-boolean body value must not silently turn a preview into a live dispatch."""
    seen = _post({"task_id": "t", "dry_run": value})
    assert seen["dry_run"] is True


def test_absent_dry_run_body_keeps_preview_default():
    assert _post({"task_id": "t"})["dry_run"] is True


def test_no_body_keeps_preview_default():
    assert _post(None)["dry_run"] is True


def test_explicit_json_false_is_honoured():
    assert _post({"dry_run": False})["dry_run"] is False


def test_explicit_json_true_is_honoured():
    assert _post({"dry_run": True})["dry_run"] is True


def test_query_false_is_honoured_when_body_has_no_dry_run():
    seen = _post({"task_id": "t"}, query="?dry_run=false")
    assert seen["dry_run"] is False


def test_query_false_is_kept_when_body_dry_run_is_non_boolean():
    # The query explicitly asks for a live dispatch; a malformed body value
    # must not change that (only an explicit body boolean overrides it).
    seen = _post({"dry_run": None}, query="?dry_run=false")
    assert seen["dry_run"] is False
