import csv
from datetime import UTC, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from typing import Annotated, Any, Self, cast

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncResult, AsyncSession

from api.sema.property_format import is_left_right_dot
from api.v2.dependencies import get_current_username, get_db


router = APIRouter()


class HourlyElectricityQueryParams(BaseModel):
    start: datetime
    end: datetime
    dl: bool | None = None

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
        writer.writerow([
            cast(datetime, row_list[0]).astimezone(ZoneInfo("America/New_York")).strftime('%m/%d/%Y %H:%M'),
            f"{cast(float, row_list[1]):.2f}"
        ])
        yield buffer.getvalue()
        
        # Clear the buffer after each write to keep memory footprint flat
        buffer.seek(0)
        buffer.truncate(0)


@router.get("/api/v2/installations/{installation_id_param}/hourly.electricity")
async def get_hourly_electricity(
    installation_id_param: str,
    query: Annotated[HourlyElectricityQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):

    is_old_data = (datetime.now(tz=UTC) - timedelta(days=10)) > query.end
    installation_aliases = [x for x in installation_id_param.split(',') if is_left_right_dot(x)]

    sql = text("""
        SELECT time_bucket,SUM(hp_kwh_el),MAX(total_usd_per_mwh)
        FROM gridworks.cached_hourly_data
        JOIN gridworks.g_nodes ON (g_nodes.alias || '.ta') = cached_hourly_data.terminal_asset_alias
        JOIN gridworks.installations ON installations.g_node_id = g_nodes.id
        JOIN gridworks.user_installation_roles ON ((user_installation_roles.installation_id = installations.id) OR (user_installation_roles.installation_id IS NULL))
        JOIN gridworks.users ON users.id = user_installation_roles.user_id
        WHERE time_bucket >= :t_start
        AND time_bucket <= :t_end
        AND username = :username
        AND (role = 'owner' OR role = 'admin' OR :is_old_data)
        AND g_nodes.alias IN :aliases
        GROUP BY time_bucket
        ORDER BY time_bucket
    """)

    sql = sql.bindparams(bindparam("aliases", expanding=True))

    db_results = await db.stream(sql, {
        "t_start": query.start,
        "t_end": query.end,
        "username": username,
        "is_old_data": is_old_data,
        "aliases": installation_aliases
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

    else:
        all_rows = await db_results.all()
        return [[x[0], x[1], x[2]] for x in all_rows]

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
