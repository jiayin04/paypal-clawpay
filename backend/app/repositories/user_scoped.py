"""Per-request PostgREST reads with the caller's Supabase JWT (RLS-enforced).

Column lists are explicit; RLS and column grants scope rows and hide columns
even if a filter here were wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import anyio
import httpx
from postgrest import SyncPostgrestClient
from postgrest.exceptions import APIError

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailable, Unauthorized
from app.integrations.supabase.client import create_user_postgrest
from app.schemas.dashboard import (
    A2A_COLUMNS,
    AUDIT_COLUMNS,
    CONVERSATION_COLUMNS,
    EVENT_COLUMNS,
    METHOD_COLUMNS,
    public_rows,
)


ORDER_LIST_COLUMNS = "id,merchant_id,currency,total_minor,status,quote_expires_at,created_at,updated_at"
ORDER_DETAIL_COLUMNS = ORDER_LIST_COLUMNS + ",subtotal_minor,device_id"
ORDER_ITEM_COLUMNS = "product_reference,description,quantity,unit_price_minor,line_total_minor"
APPROVAL_COLUMNS = "approval_type,decision,verification_status,key_version,approved_at,consumed_at"
PAYMENT_ATTEMPT_COLUMNS = "status,amount_minor,currency,failure_code,created_at,updated_at"
DEVICE_COLUMNS = "id,device_name,key_algorithm,key_version,status,created_at,revoked_at,last_seen_at"


class PostgrestUserReader:
    def __init__(self, client: SyncPostgrestClient) -> None:
        self._client = client

    async def _run(self, fn: Callable[[SyncPostgrestClient], Any]) -> list[dict[str, Any]]:
        try:
            response = await anyio.to_thread.run_sync(lambda: fn(self._client))
        except APIError as exc:
            if str(exc.code) in ("401", "PGRST301", "PGRST302", "PGRST303"):
                raise Unauthorized() from None
            raise ServiceUnavailable() from None
        except httpx.HTTPError:
            raise ServiceUnavailable() from None
        return list(response.data or [])

    async def list_orders(self, *, limit: int, before: datetime | None) -> list[dict[str, Any]]:
        def query(c: SyncPostgrestClient) -> Any:
            q = c.from_("orders").select(ORDER_LIST_COLUMNS)
            if before is not None:
                q = q.lt("created_at", before.isoformat())
            return q.order("created_at", desc=True).limit(limit).execute()

        return await self._run(query)

    async def get_order_detail(self, order_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            lambda c: c.from_("orders").select(ORDER_DETAIL_COLUMNS).eq("id", order_id).limit(1).execute()
        )
        if not rows:
            return None
        order = rows[0]
        order["items"] = await self._run(
            lambda c: c.from_("order_items").select(ORDER_ITEM_COLUMNS).eq("order_id", order_id).execute()
        )
        order["approvals"] = await self._run(
            lambda c: c.from_("approvals").select(APPROVAL_COLUMNS).eq("order_id", order_id).execute()
        )
        order["payment_attempts"] = await self._run(
            lambda c: c.from_("payment_attempts")
            .select(PAYMENT_ATTEMPT_COLUMNS)
            .eq("order_id", order_id)
            .execute()
        )
        merchants = await self._run(
            lambda c: c.from_("merchants")
            .select("display_name")
            .eq("id", order["merchant_id"])
            .limit(1)
            .execute()
        )
        order["merchant"] = merchants[0]["display_name"] if merchants else None
        mandates = await self._run(
            lambda c: c.from_("mandates").select("status").eq("order_id", order_id).limit(1).execute()
        )
        order["mandate_status"] = mandates[0]["status"] if mandates else None
        return order

    async def list_devices(self) -> list[dict[str, Any]]:
        return await self._run(
            lambda c: c.from_("devices").select(DEVICE_COLUMNS).order("created_at", desc=True).execute()
        )

    async def page(self, table, columns, *, limit=20, before=None, filters=None):
        def query(c):
            q = c.from_(table).select(columns)
            for key,value in (filters or {}).items():
                q = q.eq(key,value)
            if before is not None:
                q = q.lt('created_at',before.isoformat())
            return q.order('created_at',desc=True).limit(limit).execute()
        return public_rows(await self._run(query))

    async def list_payment_methods(self):
        return await self._run(lambda c: c.from_('payment_methods').select(METHOD_COLUMNS).order('created_at',desc=True).execute())

    async def list_conversations(self, *, limit, before):
        return await self.page('conversations',CONVERSATION_COLUMNS,limit=limit,before=before)

    async def get_conversation(self, conversation_id):
        rows = await self._run(lambda c: c.from_('conversations').select('id').eq('id',conversation_id).limit(1).execute())
        return rows[0] if rows else None

    async def conversation_events(self, conversation_id, *, limit, before):
        return await self.page('conversation_events',EVENT_COLUMNS,filters={'conversation_id':conversation_id},limit=limit,before=before)

    async def a2a_messages(self, order_id, *, limit, before):
        return await self.page('a2a_messages',A2A_COLUMNS,filters={'order_id':order_id},limit=limit,before=before)

    async def audit_events(self, *, limit, before):
        return await self.page('audit_logs',AUDIT_COLUMNS,limit=limit,before=before)

    async def timeline(self, order_id, *, limit, before):
        sources = [('conversation_events',EVENT_COLUMNS,{'order_id':order_id}),
                   ('a2a_messages',A2A_COLUMNS,{'order_id':order_id}),
                   ('approvals','id,'+APPROVAL_COLUMNS+',created_at',{'order_id':order_id}),
                   ('payment_attempts','id,'+PAYMENT_ATTEMPT_COLUMNS,{'order_id':order_id}),
                   ('audit_logs',AUDIT_COLUMNS,{'entity_type':'order','entity_id':order_id})]
        result = []
        for table,cols,filters in sources:
            rows = await self.page(table,cols,filters=filters,limit=limit,before=before)
            result.extend({'source':table,**r} for r in rows)
        return sorted(result,key=lambda r:r['created_at'],reverse=True)[:limit]


def make_user_reader_factory(
    settings: Settings,
    http_client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=10),
) -> Callable[[str], Any]:
    """``factory(jwt)`` is an async context manager yielding a reader.

    The ``httpx.Client`` is closed when the context exits, including on error.
    """

    @asynccontextmanager
    async def factory(jwt: str) -> AsyncIterator[PostgrestUserReader]:
        http = http_client_factory()
        try:
            yield PostgrestUserReader(create_user_postgrest(settings, jwt, http))
        finally:
            http.close()

    return factory
