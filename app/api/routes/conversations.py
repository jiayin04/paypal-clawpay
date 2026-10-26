from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import ServicesDep, require_user
from app.core.exceptions import ApiError
from app.services.context import UserContext


router = APIRouter(prefix='/api/v1/conversations',tags=['dashboard'])

@router.get('')
async def conversations(user: Annotated[UserContext,Depends(require_user)],services: ServicesDep,
                        limit: Annotated[int,Query(ge=1,le=100)] = 20,before: datetime | None = None):
    async with services.user_reader(user.jwt) as reader:
        return {'conversations':await reader.list_conversations(limit=limit,before=before)}

@router.get('/{conversation_id}/events')
async def events(conversation_id: str,user: Annotated[UserContext,Depends(require_user)],services: ServicesDep,
                 limit: Annotated[int,Query(ge=1,le=100)] = 20,before: datetime | None = None):
    try:
        conversation_id = str(UUID(conversation_id))
    except ValueError:
        raise ApiError(404,'conversation_not_found','Conversation not found.') from None
    async with services.user_reader(user.jwt) as reader:
        if not await reader.get_conversation(conversation_id):
            raise ApiError(404,'conversation_not_found','Conversation not found.')
        return {'events':await reader.conversation_events(conversation_id,limit=limit,before=before)}
