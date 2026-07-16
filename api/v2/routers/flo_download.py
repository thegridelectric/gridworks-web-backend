from datetime import datetime, timedelta
import gc
import os
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from gridflo import DGraphVisualizer, Flo
from pydantic import BaseModel
from sqlalchemy import String, cast, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import get_db, require_sys_admin_username

from gw_data.db.models import (
    MessageSql,
)

router = APIRouter()

class FloDataQueryParams(BaseModel):
    time: datetime


@router.get("/api/v2/installations/{installation_id}/flo.download")
async def get_messages(
    installation_id: str,
    query: Annotated[FloDataQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_sys_admin_username)
):
    db_query = (
        select(cast(MessageSql.payload, String))
        .order_by(MessageSql.timestamp)
        .filter(
            MessageSql.message_type_name == "flo.params.house0",
            MessageSql.from_alias == installation_id,
            MessageSql.timestamp >= query.time - timedelta(days = 2),
            MessageSql.timestamp <= query.time,
        )
        .order_by(desc(MessageSql.timestamp))
        .limit(1)
    )

    db_result = await db.execute(db_query)
    flo_result = db_result.scalars().one_or_none()

    if not flo_result:
        raise ValueError('No results found')

    print("Running FLO and saving analysis to excel...")
    g = Flo(flo_result.encode())
    g.solve_dijkstra()
    v = DGraphVisualizer(g)
    v.export_to_excel()
    del g 
    del v
    gc.collect()
    print("Done.")

    if os.path.exists('result.xlsx'):
        return FileResponse(
            'result.xlsx',
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={"Content-Disposition": "attachment; filename=file.xlsx"}
            )

    raise ValueError('Result xls file generation failed')