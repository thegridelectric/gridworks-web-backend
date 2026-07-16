from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec

from ..dependencies import get_db, require_sys_admin_username

from gw_data.db.models import (
    MessageSql,
)

router = APIRouter()


class MessagesQueryParams(BaseModel):
    start: datetime
    end: datetime
    message_types: str = Field("")

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end_time must be after start_time")
        return self

ALLOWED_MESSAGE_TYPES = {
    'weather.forecast',
    'glitch',
    'gridworks.event.problem'
}

@router.get("/api/v2/installations/{installation_id_param}/messages")
async def get_messages(
    installation_id_param: str,
    query: Annotated[MessagesQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_sys_admin_username)
):
    db_message_types = ALLOWED_MESSAGE_TYPES.intersection(query.message_types.split(','))
    if installation_id_param == '*':
        installation_id_filter = []
    else:
        installation_ids = installation_id_param.split(',')
        installation_id_filter = map(lambda x: MessageSql.from_alias.like(f'%{x}%'), installation_ids)

    db_query = (
        select(MessageSql.payload)
        .order_by(MessageSql.timestamp)
        .filter(
            MessageSql.timestamp >= query.start,
            MessageSql.timestamp <= query.end,
            MessageSql.message_type_name.in_(db_message_types),
            or_(*installation_id_filter)
        )
        .order_by(MessageSql.timestamp)
        .limit(100)
    )

    db_result = await db.execute(db_query)
    rows = db_result.all()

    codec = SemaCodec()
    sema_results = [codec.from_dict(x[0]) for x in rows]
    return sema_results
