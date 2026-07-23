import csv
from datetime import datetime, timedelta
import io
import math
import re
from typing import Annotated, Self, cast
from zoneinfo import ZoneInfo
from aioitertools.itertools import islice
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings
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
def is_not_regex(x):
    return not is_regex(x)

def determine_query_channels(channels: list[str]):

    if 'all-data' in channels:
        return (None, None)
    
    str_channels = set(filter(is_not_regex, channels))
    regexp_channels = set(filter(is_regex, channels))

    return str_channels, regexp_channels

async def csv_generator(
    readings_query_stream_result,
    reading_channel_names,
    request_query: ReadingsDownloadQueryParams,
    addl_headers: list[str],
):

    requested_channels = request_query.channels.split(",")

    # readings_metadata is a list of
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(addl_headers)

    # reading_channel_names will be in alphabetical order. We want our CSV column order to match the request.
    sorted_channel_names = []
    if "all-data" in requested_channels:
        sorted_channel_names.extend(reading_channel_names)
    else:
        for c in requested_channels:
            if is_regex(c):
                re_results: list[tuple[str, re.Match[str]]] = [
                    r
                    for r in [
                        (name, re.fullmatch(c, name)) for name in reading_channel_names
                    ]
                    if r[1] is not None
                ]  # type: ignore
                # Sort by the regex capture groups first, numerically if possible
                re_results.sort(
                    key=lambda r: (
                        *([int(g) if g.isdigit() else g for g in r[1].groups()]),
                        r[0],
                    )
                )
                matches = [r[0] for r in re_results]
            else:
                matches = list(filter(lambda name: c == name, reading_channel_names))
            sorted_channel_names.extend(matches)

    writer.writerow(["timestamp", *sorted_channel_names])

    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    readings_query_stream_iterator = aiter(readings_query_stream_result)
    current_reading_row = await anext(readings_query_stream_iterator, None)
    if not current_reading_row:
        return

    first_result_time_offset_s = (cast(datetime, current_reading_row[0]) - request_query.start).seconds
    if first_result_time_offset_s > 0:
        row_time_start = request_query.start
    else:
        bucketed_time_offset_s = request_query.time_step * (first_result_time_offset_s // request_query.time_step)
        row_time_start = request_query.start + timedelta(seconds=bucketed_time_offset_s)

    row_counter = 0
    prev_row_values: list[float | None] | None = None
    while row_time_start <= request_query.end:
        row_time_end = row_time_start + timedelta(seconds=request_query.time_step)

        # Each reading is time, name, value
        # Collect all the readings that apply to this row of the CSV
        # For each column, find the average value. If there are no readings for the column, copy its value from the previous row.

        current_row_readings: dict[str, list[int]] = {}
        while current_reading_row is not None and current_reading_row[0] < row_time_end:
            reading_ch_name = current_reading_row[1]
            reading_value = current_reading_row[2]
            if reading_ch_name in current_row_readings:
                current_row_readings[reading_ch_name].append(reading_value)
            else:
                current_row_readings[reading_ch_name] = [reading_value]

            current_reading_row = await anext(readings_query_stream_iterator, None)

        row_values = [
            int(sum(current_row_readings[ch_name]) / len(current_row_readings[ch_name]))
            if ch_name in current_row_readings
            else (prev_row_values[index] if prev_row_values else None)
            for index, ch_name in enumerate(sorted_channel_names)
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


@router.get('/api/v2/installations/{installation_id}/readings.download')
async def get_readings(
    installation_id, 
    query: Annotated[ReadingsDownloadQueryParams, Query()], 
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    await verify_data_access(db, username, installation_id, effective_date=query.end)

    channels = query.channels.split(',')
    str_channels, regexp_channels = determine_query_channels(channels)

    # The time range on our query needs to include the entire last bucket (which is later than the requested end time).
    db_query_end = query.end + timedelta(seconds=query.time_step)

    reading_query_filters = [
        ReadingSql.timestamp >= query.start,
        ReadingSql.timestamp <= db_query_end,
        ReadingChannelSql.terminal_asset_alias == installation_id + ".ta"
    ]

    if str_channels is not None and regexp_channels is not None:
        reading_query_filters.append(or_(
            ReadingChannelSql.name.in_(str_channels),
            *[ReadingChannelSql.name.regexp_match(r) for r in regexp_channels]
        ))

    reading_channel_names_query = select(
        ReadingChannelSql.name
    ).distinct().join(ReadingSql).filter(
        *reading_query_filters
    ).order_by(ReadingChannelSql.name)
    reading_channel_names_result = await db.execute(reading_channel_names_query)
    reading_channel_names = list(reading_channel_names_result.scalars().all())

    # To implement Last-Observation-Carried-Forward on our readings, we need to query for data a little bit earlier than what
    # was actually requested (so we have a value to carry forward).
    # There is no good way to know how far back to search, so we just go 30 minutes (rounded to the nearest bucket).
    early_reading_duration_s = query.time_step * (60 * 30 // query.time_step)
    db_query_start = query.start - timedelta(seconds=early_reading_duration_s)

    # The readings query could return millions of rows, so we need to stream the result set without loading
    # the entire thing into memory. I was unable to get this to work via SQLAlchemy, so this code uses the psycopg2
    # connection itself. This means we need to manually build the SQL query and a list of arguments.

    connection = await db.connection()
    raw_connection = await connection.get_raw_connection()
    conn = raw_connection.driver_connection

    db_query_args = [
        db_query_start, 
        db_query_end, 
        installation_id + '.ta', 
    ]

    channel_query_clause = ""
    if str_channels is not None and regexp_channels is not None:
        offset = 1 + len(db_query_args)
        str_channel_arg_list = [f'${i + offset}' for i,val in enumerate(str_channels)]
        db_query_args.extend(str_channels)
        exact_channel_clause = f'reading_channels.name IN ({','.join(str_channel_arg_list)})'

        offset = 1 + len(db_query_args)
        regexp_channel_clause = ' '.join([f'OR reading_channels.name ~ ${i + offset}' for i,val in enumerate(regexp_channels)])
        db_query_args.extend(regexp_channels)

        channel_query_clause += f'AND ({exact_channel_clause} {regexp_channel_clause})'

    readings_query = """
        SELECT gridworks.readings.timestamp, gridworks.reading_channels.name, gridworks.readings.value 
        FROM gridworks.readings
        JOIN gridworks.reading_channels ON reading_channels.id = readings.channel_id
        WHERE gridworks.readings.timestamp >= $1
        AND gridworks.readings.timestamp <= $2
        AND gridworks.reading_channels.terminal_asset_alias = $3
    """ + channel_query_clause + """
        ORDER BY gridworks.readings.timestamp, gridworks.reading_channels.name
    """

    async with conn.transaction(): # type: ignore

        readings_query_stream_result = conn.cursor(readings_query, *db_query_args, prefetch=10000) # type: ignore

        formatted_start_date = query.start.isoformat()[:16].replace('T', '_').replace(':', '').replace('-', '')
        formatted_end_date = query.end.isoformat()[:16].replace('T', '_')
        installation_alias = installation_id.split('.')[-1]
        filename = f'{installation_alias}_{query.time_step}s_{formatted_start_date}-{formatted_end_date}.csv'

        return StreamingResponse(
            csv_generator(
                readings_query_stream_result,
                reading_channel_names,
                request_query = query,
                addl_headers=[filename, installation_id],
            ),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
