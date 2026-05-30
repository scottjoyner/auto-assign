from auto_assign.cache import CacheStore
from auto_assign.clients import AssistXClient, RouterClient
from auto_assign.models import AssignmentCandidate
from auto_assign.scorer import AssignmentScorer
from auto_assign.service import AssignmentService
from auto_assign.settings import Settings


class FakeAssistXClient(AssistXClient):
    async def get_task(self, task_id: str):
        if task_id == "ASS-approval-event":
            return AssignmentCandidate(task_id=task_id, approval_required=True)
        if task_id == "ASS-skipped-event":
            return AssignmentCandidate(task_id=task_id, allowed_lanes=[])
        return None

    async def event_status(self, idempotency_key: str):
        return {"known": False}


class FakeRouterClient(RouterClient):
    async def snapshot(self):
        from auto_assign.models import RouterSnapshot

        return RouterSnapshot(reachable=True, context_revision="event-type-test")


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'events.sqlite3'}")
    return AssignmentService(
        settings=settings,
        cache=CacheStore(settings.sqlite_path),
        assistx=FakeAssistXClient(settings),
        router=FakeRouterClient(settings),
        scorer=AssignmentScorer(settings),
    )


async def test_approval_required_decision_emits_approval_required_event(tmp_path):
    service = make_service(tmp_path)

    decision = await service.evaluate(
        __import__("auto_assign.models", fromlist=["AssignmentEvaluateRequest"]).AssignmentEvaluateRequest(
            task_id="ASS-approval-event"
        )
    )

    events = service.list_outbox_events()
    assert decision.status == "approval_required"
    assert events[0]["payload"]["event_type"] == "assign.assignment.approval_required"
    assert events[0]["payload"]["idempotency_key"].startswith("assign.assignment.decision:")


async def test_blocked_nonapproval_decision_emits_skipped_event(tmp_path):
    service = make_service(tmp_path)

    decision = await service.evaluate(
        __import__("auto_assign.models", fromlist=["AssignmentEvaluateRequest"]).AssignmentEvaluateRequest(
            task_id="ASS-skipped-event",
            candidate_lanes=[],
        )
    )

    events = service.list_outbox_events()
    assert decision.status == "blocked"
    assert events[0]["payload"]["event_type"] == "assign.assignment.skipped"
    assert events[0]["payload"]["idempotency_key"].startswith("assign.assignment.decision:")
