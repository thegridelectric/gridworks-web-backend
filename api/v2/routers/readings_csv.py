import csv
from datetime import datetime, timedelta
import io
import math
import re
from typing import Annotated, Self, cast
from uuid import UUID
from zoneinfo import ZoneInfo
from aioitertools.itertools import islice
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings
from ..readings_units import ChannelUnit, Column, ColumnValue, UnitConflict, column_values, lineages
from ..shared_queries import verify_data_access

from ..dependencies import get_current_username, get_db, get_settings
from gw_data.db.models import (
    ReadingChannelSql,
    ReadingSql,
)

TZ_NEW_YORK = ZoneInfo('America/New_York')

router = APIRouter()

class ReadingsDownloadQueryParams(BaseModel):
    start: datetime
    end: datetime
    time_step: int
    channels: str = Field('')

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end_time must be after start_time")
        return self

def is_regex(x):
    return len(x) >= 2 and x[0] == '^' and x[-1] == '$'

def select_columns(columns: list[Column], requested_channels: list[str]) -> list[Column]:
    """The columns a request names, in the request's order. An exact name
    matches a column by any name its lineage has had, so a reader asking by
    the old name of a renamed channel gets the column; a regex (`^...$`)
    likewise, with its matches sorted by capture group, numerically where
    the group is digits. `all-data` is every column by name."""
    if "all-data" in requested_channels:
        return sorted(columns, key=lambda c: c.name)
    selected: list[Column] = []
    for c in requested_channels:
        if is_regex(c):
            matched: list[tuple[re.Match[str], Column]] = []
            for column in columns:
                for name in sorted(column.names, key=lambda n: n != column.name):
                    m = re.fullmatch(c, name)
                    if m is not None:
                        matched.append((m, column))
                        break
            matched.sort(key=lambda r: (*[int(g) if g.isdigit() else g for g in r[0].groups()], r[1].name))
            selected.extend(column for _, column in matched)
        else:
            selected.extend(column for column in columns if c in column.names)
    return selected


async def csv_generator(
    readings_query_stream_result,
    columns: list[Column],
    cell_rules: dict[UUID, ColumnValue],
    request_query: ReadingsDownloadQueryParams,
    addl_headers: list[str],
):
    """One column per channel lineage. Its rows (a layout change in unit, a
    rename, an enum version) feed the column through `cell_rules`, which
    converts every value into the unit of the newest row; a state column's
    cells are state symbols. A bucket's cell is the mean of its numeric
    readings, the last of its states, or the previous row's cell."""

    column_names = [c.name for c in columns]
    state_columns = {rule.column for rule in cell_rules.values() if rule.is_state}

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(addl_headers)
    writer.writerow(["timestamp", *column_names])

    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    readings_query_stream_iterator = aiter(readings_query_stream_result)
    current_reading_row = await anext(readings_query_stream_iterator, None)
    if not current_reading_row:
        return

    # The first bucket starts at the first reading's bucket, which is before the requested start when the
    # carry-forward look-back found data; buckets before the start are computed and not written.
    first_result_time_offset_s = (cast(datetime, current_reading_row[0]) - request_query.start).total_seconds()
    if first_result_time_offset_s > 0:
        row_time_start = request_query.start
    else:
        bucketed_time_offset_s = request_query.time_step * math.floor(first_result_time_offset_s / request_query.time_step)
        row_time_start = request_query.start + timedelta(seconds=bucketed_time_offset_s)

    row_counter = 0
    prev_row_values: list[float | str | None] | None = None
    while row_time_start <= request_query.end:
        row_time_end = row_time_start + timedelta(seconds=request_query.time_step)

        # Each reading is time, row id, value. Collect the readings that fall in this bucket, by column.

        current_row_readings: dict[str, list[float | str]] = {}
        while current_reading_row is not None and current_reading_row[0] < row_time_end:
            rule = cell_rules[current_reading_row[1]]
            current_row_readings.setdefault(rule.column, []).append(rule.convert(current_reading_row[2]))

            current_reading_row = await anext(readings_query_stream_iterator, None)

        row_values: list[float | str | None] = [
            bucket_cell(current_row_readings[ch_name], ch_name in state_columns)
            if ch_name in current_row_readings
            else (prev_row_values[index] if prev_row_values else None)
            for index, ch_name in enumerate(column_names)
        ]
        if row_time_start >= request_query.start:
            buffer.write(row_time_start.astimezone(TZ_NEW_YORK).strftime("%Y-%m-%d %H:%M:%S"))
            for v in row_values:
                buffer.write(',')
                buffer.write(str(v))

            buffer.write('\n')
            row_counter += 1
            # Clear the buffer after batches of rows to keep memory footprint flat
            if row_counter % 1000 == 0:
                yield buffer.getvalue()

                buffer.seek(0)
                buffer.truncate(0)

        prev_row_values = row_values
        row_time_start = row_time_end

    yield buffer.getvalue()


def bucket_cell(values: list[float | str], is_state: bool) -> float | str:
    if is_state:
        return values[-1]
    numbers = cast(list[float], values)
    return round(sum(numbers) / len(numbers))


@router.get('/api/v2/installations/{installation_id}/readings.download')
async def get_readings(
    installation_id, 
    query: Annotated[ReadingsDownloadQueryParams, Query()], 
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    await verify_data_access(db, username, installation_id, effective_date=query.end)

    # The time range on our query needs to include the entire last bucket (which is later than the requested end time).
    db_query_end = query.end + timedelta(seconds=query.time_step)

    # To implement Last-Observation-Carried-Forward on our readings, we need to query for data a little bit earlier than what
    # was actually requested (so we have a value to carry forward).
    # There is no good way to know how far back to search, so we just go 30 minutes (rounded to the nearest bucket).
    early_reading_duration_s = query.time_step * (60 * 30 // query.time_step)
    db_query_start = query.start - timedelta(seconds=early_reading_duration_s)

    # Every row of the house with a reading in the window: the lineages are knit from all of them, and the
    # request then picks columns, so a column asked for by one of its names comes whole.
    reading_channels_query = select(
        ReadingChannelSql.id, ReadingChannelSql.name, ReadingChannelSql.unit_type, ReadingChannelSql.unit,
        ReadingChannelSql.deactivated_date, ReadingChannelSql.channel_id,
    ).distinct().join(ReadingSql).filter(
        ReadingSql.timestamp >= db_query_start,
        ReadingSql.timestamp <= db_query_end,
        ReadingChannelSql.terminal_asset_alias == installation_id + ".ta",
    )
    reading_channels_result = await db.execute(reading_channels_query)
    columns = select_columns(lineages([ChannelUnit(*row) for row in reading_channels_result.all()]), query.channels.split(','))
    try:
        cell_rules = column_values(columns)
    except UnitConflict as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e

    # The readings query could return millions of rows, so we need to stream the result set without loading
    # the entire thing into memory. I was unable to get this to work via SQLAlchemy, so this code uses the driver
    # connection itself, with the SQL and its arguments built by hand.

    connection = await db.connection()
    raw_connection = await connection.get_raw_connection()
    conn = raw_connection.driver_connection

    readings_query = """
        SELECT gridworks.readings.timestamp, gridworks.readings.channel_id, gridworks.readings.value
        FROM gridworks.readings
        WHERE gridworks.readings.timestamp >= $1
        AND gridworks.readings.timestamp <= $2
        AND gridworks.readings.channel_id = ANY($3::uuid[])
        ORDER BY gridworks.readings.timestamp
    """
    db_query_args = [db_query_start, db_query_end, list(cell_rules)]

    async with conn.transaction(): # type: ignore

        readings_query_stream_result = conn.cursor(readings_query, *db_query_args, prefetch=10000) # type: ignore

        formatted_start_date = query.start.isoformat()[:16].replace('T', '_').replace(':', '').replace('-', '')
        formatted_end_date = query.end.isoformat()[:16].replace('T', '_')
        installation_alias = installation_id.split('.')[-1]
        filename = f'{installation_alias}_{query.time_step}s_{formatted_start_date}-{formatted_end_date}.csv'

        return StreamingResponse(
            csv_generator(
                readings_query_stream_result,
                columns,
                cell_rules,
                request_query = query,
                addl_headers=[filename, installation_id],
            ),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
