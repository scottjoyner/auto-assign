from auto_assign.clients import AssistXClient
from auto_assign.models import Lane
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
