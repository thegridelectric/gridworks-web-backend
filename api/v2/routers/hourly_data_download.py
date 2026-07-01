import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import get_db

router = APIRouter()


class HourlyDataDownloadQueryParams(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end must be after start")
        return self

async def database_row_generator(
    installation_id: str,
    query: Annotated[HourlyDataDownloadQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
):
    sql = text("SELECT to_char(time_bucket AT TIME ZONE 'America/New_York', 'MM/DD/YYYY HH24:MI') AS hour_start,* FROM gridworks.cached_hourly_data WHERE time_bucket >= :t_start AND time_bucket <= :t_end AND terminal_asset_alias = :ta ORDER BY time_bucket")
    
    db_results = await db.stream(sql, {
        "t_start": query.start,
        "t_end": query.end,
        "ta": installation_id + ".ta"
    })

    buffer = StringIO()
    writer = csv.writer(buffer)

    headers = list(db_results.keys())
    writer.writerow(headers[0:1] + headers[3:])
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    # Iterate over the stream chunk-by-chunk
    async for row in db_results:        
        row_list = list(row)
        writer.writerow(row_list[0:1] + row_list[3:])
        yield buffer.getvalue()
        
        # Clear the buffer after each write to keep memory footprint flat
        buffer.seek(0)
        buffer.truncate(0)

@router.get("/api/v2/installations/{installation_id}/hourly.data")
async def get_data(
    installation_id: str,
    query: Annotated[HourlyDataDownloadQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
):
    # TODO authorization for the installations

    formatted_start_date = query.start.strftime('%Y-%m-%d-%H-%M')
    formatted_end_date = query.end.strftime('%Y-%m-%d-%H-%M')
    installation_alias = installation_id.split('.')[-1]
    filename = f'{installation_alias}_electricity_use_{formatted_start_date}-{formatted_end_date}.csv'

    return StreamingResponse(
        database_row_generator(installation_id, query, db), 
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )        