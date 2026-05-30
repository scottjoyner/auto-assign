from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.models import Lane, RouterSnapshot
from auto_assign.settings import Settings


def test_assistx_client_extracts_tasks_shape() -> None:
    client = AssistXClient(Settings())

    items = client._extract_items({"tasks": [{"task_id": "t1"}]})

    assert items == [{"task_id": "t1"}]


def test_assistx_client_extracts_legacy_candidate_shapes() -> None:
    client = AssistXClient(Settings())

    assert client._extract_items({"candidates": [{"task_id": "t1"}]}) == [{"task_id": "t1"}]
    assert client._extract_items({"items": [{"task_id": "t2"}]}) == [{"task_id": "t2"}]
    assert client._extract_items([{"task_id": "t3"}]) == [{"task_id": "t3"}]


def test_candidate_normalization_maps_id_and_privacy() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item(
        {
            "id": "assistx-task-1",
            "title": "Private work",
            "privacy": "private",
            "allowed_lanes": ["free_api", "local_only"],
        }
    )

    assert candidate.task_id == "assistx-task-1"
    assert candidate.local_only is True
    assert candidate.sensitive is True
    assert candidate.allow_cloud is False
    assert "private" in candidate.privacy_labels
    assert candidate.allowed_lanes == [Lane.FREE_API, Lane.LOCAL_ONLY]


def test_candidate_normalization_keeps_safe_cloud_allowed() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item({"task_id": "safe", "allow_cloud": True})

    assert candidate.local_only is False
    assert candidate.sensitive is False
    assert candidate.allow_cloud is True


def test_candidate_from_item_accepts_string_privacy_labels() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item(
        {
            "id": "ASS-string-labels",
            "privacy_labels": "local_only,voice_auth",
            "allow_cloud": "true",
        }
    )

    assert candidate.task_id == "ASS-string-labels"
    assert candidate.local_only is True
    assert candidate.sensitive is True
    assert candidate.allow_cloud is False
    assert set(candidate.privacy_labels) >= {"local_only", "voice_auth"}


def test_candidate_from_item_preserves_false_string_booleans() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item(
        {
            "task_id": "ASS-false-strings",
            "local_only": "false",
            "sensitive": "false",
            "allow_cloud": "false",
        }
    )

    assert candidate.local_only is False
    assert candidate.sensitive is False
    assert candidate.allow_cloud is False


def test_candidate_from_item_infers_privacy_from_metadata() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item(
        {
            "uuid": "ASS-metadata-privacy",
            "metadata": {"privacy": "secret"},
        }
    )

    assert candidate.task_id == "ASS-metadata-privacy"
    assert candidate.local_only is True
    assert candidate.sensitive is True
    assert candidate.allow_cloud is False
    assert "secret" in candidate.privacy_labels


def test_candidate_from_item_supports_semicolon_privacy_labels() -> None:
    client = AssistXClient(Settings())

    candidate = client._candidate_from_item(
        {
            "task_id": "ASS-semicolon",
            "privacy_labels": "private_data;enrollment_sample",
        }
    )

    assert candidate.sensitive is True
    assert set(candidate.privacy_labels) >= {"private_data", "enrollment_sample"}


def test_router_snapshot_normalizes_nested_context_payload() -> None:
    client = RouterClient(Settings())
    snapshot = RouterSnapshot()

    client._apply_snapshot_payloads(
        snapshot=snapshot,
        context_payload={
            "snapshot": {
                "metadata": {"revision": "router-rev-1"},
                "worker_nodes": [{"id": "node-1"}],
                "model_providers": [{"name": "lmstudio"}],
                "routes": [{"name": "local-chat"}],
            }
        },
        quota_payload={"metadata": {"mode": "preserve"}},
        circuits_payload={"open": []},
        clis_payload={"items": [{"name": "codex"}]},
        ops_payload={"status": "ok"},
        reachable=True,
    )

    assert snapshot.reachable is True
    assert snapshot.context_revision == "router-rev-1"
    assert snapshot.nodes == [{"id": "node-1"}]
    assert snapshot.providers == [{"name": "lmstudio"}]
    assert snapshot.services == [{"name": "local-chat"}]
    assert snapshot.quota == {"metadata": {"mode": "preserve"}}
    assert snapshot.agent_clis == [{"name": "codex"}]
    assert snapshot.ops_summary == {"status": "ok"}


def test_router_snapshot_marks_malformed_context_unreachable() -> None:
    client = RouterClient(Settings())
    snapshot = RouterSnapshot()

    client._apply_snapshot_payloads(
        snapshot=snapshot,
        context_payload=["not", "a", "mapping"],
        quota_payload=None,
        circuits_payload=None,
        clis_payload=None,
        ops_payload=None,
        reachable=True,
    )

    assert snapshot.reachable is False
    assert snapshot.nodes == []
    assert snapshot.providers == []
    assert snapshot.services == []
    assert snapshot.quota == {}


def test_router_payload_list_accepts_list_payloads() -> None:
    client = RouterClient(Settings())

    result = client._payload_list([{"name": "a"}, "bad", {"name": "b"}], ("items",))

    assert result == [{"name": "a"}, {"name": "b"}]
