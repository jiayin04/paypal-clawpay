"""In-memory dashboard adapter sharing the store transaction lock."""
from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeUserReader:
    """Simulated RLS: every read is filtered by the JWT subject."""

    def __init__(self, store: InMemoryStore, user_id: str) -> None:
        self.s = store
        self.user_id = user_id

    async def list_orders(self, *, limit: int, before: datetime | None) -> list[dict[str, Any]]:
        rows = [o for o in self.s.orders.values() if o["user_id"] == self.user_id
                and (before is None or o["created_at"] < before)]
        rows.sort(key=lambda o: o["created_at"], reverse=True)
        cols = ("id", "merchant_id", "currency", "total_minor", "status", "quote_expires_at", "created_at", "updated_at")
        return [{k: (o[k].isoformat() if isinstance(o[k], datetime) else o[k]) for k in cols} for o in rows[:limit]]

    async def get_order_detail(self, order_id: str) -> dict[str, Any] | None:
        order = self.s.orders.get(order_id)
        if order is None or order["user_id"] != self.user_id:
            return None
        cols = ("id", "merchant_id", "currency", "subtotal_minor", "total_minor", "status", "quote_expires_at",
                "created_at", "updated_at", "device_id")
        out = {k: (order[k].isoformat() if isinstance(order[k], datetime) else order[k]) for k in cols}
        out["items"] = [
            {k: i[k] for k in ("product_reference", "description", "quantity", "unit_price_minor", "line_total_minor")}
            for i in self.s.order_items if i["order_id"] == order_id
        ]
        from app.repositories.user_scoped import APPROVAL_COLUMNS, PAYMENT_ATTEMPT_COLUMNS
        out["approvals"] = self._project([r for r in self.s.approvals if r["order_id"] == order_id],APPROVAL_COLUMNS)
        out["payment_attempts"] = self._project([r for r in self.s.payment_attempts.values() if r["order_id"] == order_id],PAYMENT_ATTEMPT_COLUMNS)
        out["merchant"] = self.s.merchants[order["merchant_id"]]["display_name"]
        out["mandate_status"] = next((r["status"] for r in self.s.mandates if r["order_id"] == order_id),None)
        return out

    async def list_devices(self) -> list[dict[str, Any]]:
        cols = ("id", "device_name", "key_algorithm", "key_version", "status", "created_at", "revoked_at", "last_seen_at")
        return [
            {k: (d[k].isoformat() if isinstance(d[k], datetime) else d[k]) for k in cols}
            for d in self.s.devices.values() if d["user_id"] == self.user_id
        ]

    async def list_payment_methods(self):
        from app.schemas.dashboard import METHOD_COLUMNS
        return self._project([r for r in self.s.payment_methods if r['user_id'] == self.user_id], METHOD_COLUMNS)

    def _project(self, rows, cols, limit=100, before=None):
        from app.schemas.dashboard import public_rows
        rows = [r for r in rows if before is None or r.get('created_at',self.s.now()) < before]
        rows.sort(key=lambda r:r.get('created_at',self.s.now()),reverse=True)
        return public_rows([{k:r.get(k,self.s.now() if k == 'created_at' else None) for k in cols.split(',')} for r in rows[:limit]])

    async def list_conversations(self, *, limit, before):
        from app.schemas.dashboard import CONVERSATION_COLUMNS
        return self._project([r for r in self.s.conversations.values() if r['user_id'] == self.user_id],CONVERSATION_COLUMNS,limit,before)

    async def get_conversation(self, conversation_id):
        row = self.s.conversations.get(conversation_id)
        return {'id':conversation_id} if row and row['user_id'] == self.user_id else None

    async def conversation_events(self, conversation_id, *, limit, before):
        from app.schemas.dashboard import EVENT_COLUMNS
        if not await self.get_conversation(conversation_id):
            return []
        return self._project([r for r in self.s.conversation_events if r['conversation_id'] == conversation_id],EVENT_COLUMNS,limit,before)

    async def a2a_messages(self, order_id, *, limit, before):
        from app.schemas.dashboard import A2A_COLUMNS
        if not await self.get_order_detail(order_id):
            return []
        return self._project([r for r in self.s.a2a_messages if r.get('order_id') == order_id and r.get('user_id') == self.user_id],A2A_COLUMNS,limit,before)

    async def audit_events(self, *, limit, before):
        from app.schemas.dashboard import AUDIT_COLUMNS
        return self._project([r for r in self.s.audit_logs if r.get('user_id') == self.user_id],AUDIT_COLUMNS,limit,before)

    async def timeline(self, order_id, *, limit, before):
        from app.schemas.dashboard import EVENT_COLUMNS, A2A_COLUMNS, AUDIT_COLUMNS
        from app.repositories.user_scoped import APPROVAL_COLUMNS,PAYMENT_ATTEMPT_COLUMNS
        if not await self.get_order_detail(order_id):
            return []
        events = [r for r in self.s.conversation_events if r.get('order_id') == order_id and self.s.conversations.get(r.get('conversation_id'),{}).get('user_id') == self.user_id]
        a2a = [r for r in self.s.a2a_messages if r.get('order_id') == order_id and r.get('user_id') == self.user_id]
        approvals = [r for r in self.s.approvals if r['order_id'] == order_id]
        attempts = [r for r in self.s.payment_attempts.values() if r['order_id'] == order_id]
        audits = [r for r in self.s.audit_logs if r.get('user_id') == self.user_id and r.get('entity_id') == order_id and r.get('entity_type') == 'order']
        result = []
        for table,rows,cols in [('conversation_events',events,EVENT_COLUMNS),('a2a_messages',a2a,A2A_COLUMNS),('approvals',approvals,'id,'+APPROVAL_COLUMNS+',created_at'),('payment_attempts',attempts,'id,'+PAYMENT_ATTEMPT_COLUMNS),('audit_logs',audits,AUDIT_COLUMNS)]:
            result.extend({'source':table,**r} for r in self._project(rows,cols,limit,before))
        return sorted(result,key=lambda r:r['created_at'],reverse=True)[:limit]
