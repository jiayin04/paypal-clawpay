"""Service-role Supabase adapter for webhooks."""
from __future__ import annotations

from app.repositories.supabase.base import _SupabaseBase
from app.repositories.supabase.payments import parse_attempt_record


class SupabaseWebhookRepository(_SupabaseBase):
    async def persist_event(self, event_id, event_type, payload):
        await self._run(lambda c: c.table('webhook_events').upsert(
            {'provider':'paypal', 'event_id':event_id, 'event_type':event_type, 'payload':dict(payload)},
            on_conflict='provider,event_id', ignore_duplicates=True).execute())
        result = await self._run(lambda c: c.table('webhook_events').select('id,processed_at').eq('provider','paypal').eq('event_id',event_id).limit(1).execute())
        return result.data[0]

    async def mark_processed(self, row_id, at):
        await self._run(lambda c: c.table('webhook_events').update({'processed_at':at.isoformat()}).eq('id',row_id).execute())

    async def find_attempt(self, field, value):
        if field not in ('provider_capture_id','provider_order_id','order_id'):
            raise ValueError('Unsupported attempt lookup')
        result = await self._run(lambda c: c.table('payment_attempts').select('*').eq(field,value).limit(1).execute())
        return parse_attempt_record(result.data[0]) if result.data else None
