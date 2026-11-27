"""Hermetic tests: the emitting envelope enforces the canonical UUID contract.

The contract shim already rejects non-UUID correlation ids, but service.py
emits ``models.EventEnvelope`` directly. Without this validator a bad id
passes locally and only fails at the AssistX event boundary (or silently
breaks trace grouping).
"""
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from auto_assign.models import EventEnvelope, correlation_for_task


def envelope(**overrides):
    values = {"event_type": "assign.assignment.decision", "idempotency_key": "test-key"}
    values.update(overrides)
    return EventEnvelope(**values)


def test_default_correlation_id_is_a_valid_uuid():
    assert UUID(envelope().correlation_id)


def test_explicit_uuid_correlation_id_is_accepted():
    cid = str(uuid4())
    assert envelope(correlation_id=cid).correlation_id == cid


def test_derived_task_correlation_id_is_accepted():
    cid = correlation_for_task("task-42")
    assert UUID(cid)
    assert envelope(correlation_id=cid).correlation_id == cid


@pytest.mark.parametrize("bad", ["not-a-uuid", "corr-claim", "", "1234"])
def test_non_uuid_correlation_id_is_rejected(bad):
    with pytest.raises(ValidationError, match="correlation_id must be a valid UUID"):
        envelope(correlation_id=bad)
