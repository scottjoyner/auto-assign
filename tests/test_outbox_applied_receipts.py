"""Fail-closed acknowledgement tests for candidate in isolated source copy."""
import asyncio
import pytest
from auto_assign.models import EventEnvelope
from auto_assign.service import AssignmentService

class Cache:
    def __init__(self):
        self.delivered=[]
        self.conflicts=[]
    def pending_events(self,limit=25):
        return [EventEnvelope(event_type="assign.test",idempotency_key="synthetic-key")]
    def mark_event_delivered(self,key):
        self.delivered.append(key)
    def mark_event_failed(self,key,message,dead_letter=False):
        self.conflicts.append((key,dead_letter))
    def reconcile_delivered(self,keys):
        self.delivered.extend(keys)
        return len(keys)
    def outbox_summary(self):
        return {}

class AssistX:
    def __init__(self,status):
        self.status=status
        self.post_calls=0
    async def event_status(self,key):
        return dict(self.status)
    async def post_event(self,*args,**kwargs):
        self.post_calls+=1
        return {"delivered":False}

def run(status,method):
    service=AssignmentService.__new__(AssignmentService)
    service.cache=Cache()
    service.assistx=AssistX(status)
    result=asyncio.run(getattr(service,method)(**({"dry_run":True} if method=="dispatch_outbox" else {})))
    return service.cache.delivered,service.cache.conflicts,result,service.assistx.post_calls

@pytest.mark.parametrize("method",["dispatch_outbox","reconcile_outbox"])
@pytest.mark.parametrize("status,should_deliver",[
    ({"known":True},False),
    ({"known":True,"applied":False},False),
    ({"known":True,"applied":True},True),
    ({"known":False,"applied":True},False),
    ({"known":True,"applied":1},False),
    ({"known":1,"applied":True},False),
])
def test_exact_bool_ack_only(method,status,should_deliver):
    delivered,conflicts,result,posts=run(status,method)
    assert bool(delivered) is should_deliver
    assert not conflicts
    assert posts==0

def test_conflicting_status_not_delivered():
    delivered,conflicts,result,posts=run(
        {"known":True,"conflict":True},"reconcile_outbox")
    assert not delivered
    assert conflicts==[("synthetic-key",True)]
