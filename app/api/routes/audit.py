from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import ServicesDep, require_user
from app.services.context import UserContext


router = APIRouter(tags=['dashboard'])
@router.get('/api/v1/audit-events')
async def audits(user: Annotated[UserContext,Depends(require_user)],services: ServicesDep,
                  limit: Annotated[int,Query(ge=1,le=100)] = 20,before: datetime | None = None):
    async with services.user_reader(user.jwt) as reader:
        return {'audit_events':await reader.audit_events(limit=limit,before=before)}
