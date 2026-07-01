import csv
from datetime import datetime
from io import StringIO

from fastapi import APIRouter, Depends, Query
from typing import Annotated, Any, Self

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from api.sema.property_format import LeftRightDot, is_left_right_dot
from api.v2.dependencies import get_db


router = APIRouter()


class HourlyElectricityQueryParams(BaseModel):
    start: datetime
    end: datetime
    dl: bool

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end must be after start")
        return self

async def stream_csv(db_results: AsyncResult[Any]):

    buffer = StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["timestamp", "kwh"])
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    async for row in db_results:        
        row_list = list(row)
        writer.writerow(row_list[0:2]) # CSV skips the price
        yield buffer.getvalue()
        
        # Clear the buffer after each write to keep memory footprint flat
        buffer.seek(0)
        buffer.truncate(0)


@router.get("/api/v2/installations/{installation_ids}/messages")
async def get_hourly_electricity(
    installation_id_param: str,
    query: Annotated[HourlyElectricityQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
):
    # TODO authorization for the installations

    installation_tas = [x + ".ta" for x in installation_id_param.split(',') if is_left_right_dot(x)]

    sql = text("""
        SELECT time_bucket,SUM(hp_kwh_el),MAX(total_usd_per_mwh)
        FROM gridworks.cached_hourly_data
        WHERE time_bucket >= :t_start
        AND time_bucket <= :t_end
        AND terminal_asset_alias IN :tas
        GROUP BY time_bucket
        ORDER BY time_bucket    
    """)

    db_results = await db.stream(sql, {
        "t_start": query.start,
        "t_end": query.end,
        "tas": installation_tas
    })

    if query.dl:
        formatted_start_date = query.start.strftime('%Y-%m-%d-%H-%M')
        formatted_end_date = query.end.strftime('%Y-%m-%d-%H-%M')
        filename = f'aggregated_electricity_use_{formatted_start_date}-{formatted_end_date}.csv'

        return StreamingResponse(
            stream_csv(db_results), 
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )        

    # else:
    #     result = SyncedBundle

    #     db_query = (
    #     select(MessageSql.payload)
    #     .order_by(MessageSql.timestamp)
    #     .filter(
    #         MessageSql.timestamp >= query.start,
    #         MessageSql.timestamp <= query.end,
    #         MessageSql.message_type_name.in_(db_message_types),
    #         or_(*installation_id_filter)
    #     )
    #     .order_by(MessageSql.timestamp)
    #     .limit(100)
    # )

    # db_result = await db.execute(db_query)
    # rows = db_result.all()

    # codec = SemaCodec()
    # sema_results = [codec.from_dict(x[0]) for x in rows]
    # return sema_results
