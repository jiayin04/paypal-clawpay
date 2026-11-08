from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import ServicesDep, require_user
from app.services.context import UserContext


router = APIRouter(tags=['dashboard'])
@router.get('/api/v1/spending/summary')
async def summary(user: Annotated[UserContext,Depends(require_user)],services: ServicesDep):
    return await services.spending.tool_summary(user.user_id)
