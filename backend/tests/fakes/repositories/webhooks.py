"""In-memory webhooks adapter sharing the store transaction lock."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeWebhookRepository:
    def __init__(self, store):
        self.s = store
    async def persist_event(self,event_id,event_type,payload):
        async with self.s.lock:
            if event_id not in self.s.webhook_events:
                self.s.webhook_events[event_id] = {'id':str(uuid.uuid4()),'event_id':event_id,'event_type':event_type,'payload':dict(payload),'processed_at':None}
            return dict(self.s.webhook_events[event_id])
    async def mark_processed(self,row_id,at):
        for row in self.s.webhook_events.values():
            if row['id'] == row_id:
                row['processed_at'] = at
    async def find_attempt(self,field,value):
        for row in self.s.payment_attempts.values():
            if row.get(field) == value:
                return self.s.payments._to_record(row)
        return None
