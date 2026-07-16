import uuid

from auto_assign.contracts_shim import (
    Actor,
    AuthState,
    ContractEventEnvelope,
    EventLink,
    Lane,
)


def test_contract_envelope_requires_uuid_correlation_id():
    cid = str(uuid.uuid4())
    env = ContractEventEnvelope(
        schema_version="2026-06-08.v1",
        source_repo="auto-assign",
        event_type="assign.assignment.recommended",
        correlation_id=cid,
    )
    assert env.correlation_id == cid


def test_contract_envelope_rejects_non_uuid_correlation_id():
    try:
        ContractEventEnvelope(
            schema_version="2026-06-08.v1",
            source_repo="auto-assign",
            event_type="x",
            correlation_id="not-a-uuid",
        )
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-UUID correlation_id")


def test_contract_envelope_mirrors_links_and_actor():
    env = ContractEventEnvelope(
        schema_version="2026-06-08.v1",
        source_repo="auto-assign",
        event_type="task.candidate.created",
        correlation_id=str(uuid.uuid4()),
        actor=Actor(user_id="scott", auth_state=AuthState.AUTHENTICATED_SCOTT),
        links=[EventLink(rel="FOR_TASK", target_type="Task", target_id="T-1")],
    )
    assert env.actor.auth_state == AuthState.AUTHENTICATED_SCOTT
    assert env.links[0].target_id == "T-1"


def test_lane_enum_mirrors_contract_shape():
    assert Lane.DIRECT_WORKER.value == "direct_worker"
    assert Lane.ROUTER_MODEL.value == "router_model"
