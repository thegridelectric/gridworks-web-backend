import csv
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gw_data.db.models import GNodeSql, InstallationSql, UserInstallationRoleSql, UserSql

from api.sema.property_format import is_left_right_dot

from ..dependencies import get_current_username, get_db

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
    sql = text("""
        SELECT to_char(time_bucket AT TIME ZONE 'America/New_York', 'MM/DD/YYYY HH24:MI') AS hour_start, * 
        FROM gridworks.cached_hourly_data
        WHERE time_bucket >= :t_start       
        AND time_bucket <= :t_end
        AND terminal_asset_alias = :ta
        ORDER BY time_bucket
    """)
    
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


LeftRightDotParam = Annotated[str, BeforeValidator(is_left_right_dot)]

@router.get("/api/v2/installations/{installation_id}/hourly.data")
async def get_data(
    installation_id: Annotated[LeftRightDotParam, Path(description="The installation ID.")],
    query: Annotated[HourlyDataDownloadQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):   
    # Users with role owner or admin on the installation (or on the "null" installation)
    # can view anything. All others can only view data that is older than 10 days.

    roles_query = select(UserInstallationRoleSql).join(UserSql).outerjoin(InstallationSql).outerjoin(GNodeSql).filter(
        UserSql.username == username, 
        or_(
            GNodeSql.alias.is_(None),
            GNodeSql.alias == installation_id
        )
    )

    result = await db.execute(roles_query)
    roles = result.scalars().all()

    if len(roles) == 0:
        raise HTTPException(status_code=404)

    is_owner_admin = any(r.role in ['admin', 'owner'] for r in roles)
    is_old_data = (datetime.now(tz=UTC) - timedelta(days=10)) > query.end

    if not (is_owner_admin or is_old_data):
        raise HTTPException(status_code=404)

    formatted_start_date = query.start.strftime('%Y-%m-%d-%H-%M')
    formatted_end_date = query.end.strftime('%Y-%m-%d-%H-%M')
    installation_alias = installation_id.split('.')[-1]
    filename = f'{installation_alias}_electricity_use_{formatted_start_date}-{formatted_end_date}.csv'

    return StreamingResponse(
        database_row_generator(installation_id, query, db), 
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )        