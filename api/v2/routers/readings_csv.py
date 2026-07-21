import csv
from datetime import datetime, timedelta
import io
import math
import re
from typing import Annotated, Self
from zoneinfo import ZoneInfo
from aioitertools.itertools import islice
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings
from ..shared_queries import verify_data_access

from ..dependencies import get_current_username, get_db, get_settings

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

async def query_readings(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    time_bucket_seconds: int,
    installation_id: str,
    str_channels: set[str] | None,
    regexp_channels: set[str] | None,
):
    # To get an accurate and complete set of time-averaged data for the requested time range,
    # our query needs to include the last value from before our time range begins.
    # Otherwise, data will be missing for any of our time buckets that end before the timestamp of our first value.
    # Additionally, the first time bucket that actually does contain a value will not be able to compute an accurate
    # average value, since it won't know its starting value.
    #
    # We have no good way to know how far back to search, so we just go 30 minutes and hope it's enough.
    # Then we need to skip this many records when returning the results
    start_buffer_time_bucket_count = 60 * 30 // time_bucket_seconds
    start_buffer_s = start_buffer_time_bucket_count * time_bucket_seconds
    db_query_start = start - timedelta(seconds=start_buffer_s)

    # Additionally, we need to query for a full time bucket after our time range so that we can calculate the average value
    # of the time bucket that begins at the requested end time.
    db_query_end = end + timedelta(seconds=time_bucket_seconds)

    relevant_time_bucket_count = (
        math.floor((end - start).total_seconds() / time_bucket_seconds) + 1
    )
    db_result_time_bucket_count = relevant_time_bucket_count + start_buffer_time_bucket_count + 1

    # JDS 2026-07-21 -- After lots of trying, I could not get SQLAlchemy to actually stream results without
    # buffering it all in memory. Using the raw psycopg async connection makes this easier and faster, with the
    # downside that we have to parameterize the SQL query by hand.

    query_interval = timedelta(seconds=time_bucket_seconds)
    
    db_query_args = [
        db_query_start, 
        db_query_end, 
        installation_id + '.ta', 
        query_interval
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

    query = """
        SELECT 
            anon_1.time_bucket_gapfilled, 
            anon_1.channel_name, 
            coalesce(anon_1.avg_value, anon_1.last_reading_value) AS value
        FROM (
            SELECT 
                anon_2.channel_name AS channel_name, 
                time_bucket_gapfill($4, anon_2.time_bucket) AS time_bucket_gapfilled, 
                max(anon_2.avg_value) AS avg_value, 
                locf(max(anon_2.last_reading_value)) AS last_reading_value
            FROM (
                SELECT 
                    anon_3.channel_name AS channel_name, 
                    anon_3.time_bucket AS time_bucket, 
                    locf(last_val(anon_3.time_weight)) AS last_reading_value, 
                    interpolated_average(
                        anon_3.time_weight, 
                        time_bucket, 
                        $4, 
                        lag(anon_3.time_weight) OVER (PARTITION BY channel_name ORDER BY time_bucket), 
                        lead(anon_3.time_weight) OVER (PARTITION BY channel_name ORDER BY time_bucket)
                    ) AS avg_value
                FROM (
                    SELECT 
                        gridworks.reading_channels.name AS channel_name, 
                        time_bucket($4, gridworks.readings.timestamp) AS time_bucket, 
                        time_weight('LOCF', gridworks.readings.timestamp, gridworks.readings.value) AS time_weight
                    FROM gridworks.readings 
                    JOIN gridworks.reading_channels ON gridworks.reading_channels.id = gridworks.readings.channel_id
                    WHERE gridworks.readings.timestamp >= $1
                    AND gridworks.readings.timestamp <= $2
                    AND gridworks.reading_channels.terminal_asset_alias = $3
                    """ + channel_query_clause + """
                    GROUP BY time_bucket, gridworks.reading_channels.name 
                    ORDER BY gridworks.reading_channels.name, time_bucket
                ) AS anon_3
            ) AS anon_2
            WHERE anon_2.avg_value IS NOT NULL 
            AND anon_2.time_bucket >= $1
            AND anon_2.time_bucket <= $2
            GROUP BY anon_2.channel_name, time_bucket_gapfilled
        ) AS anon_1
        ORDER BY anon_1.time_bucket_gapfilled, anon_1.channel_name
    """

    # print(query)
    # print(db_query_args)

    connection = await db.connection()
    raw_connection = await connection.get_raw_connection()
    conn = raw_connection.driver_connection

    async with conn.transaction(): # type: ignore
        # 3. Stream rows one by one using an async for loop
        db_results = conn.cursor(query, *db_query_args, prefetch=10000) # type: ignore

        # Our result is a list of rows of (time, name, value).
        # Each time bucket will have some number of consecutive rows -- one for each channel (name) and its value in that time bucket.
        # We don't know ahead of time how many channels there will be (since we are doing regex matching).
        # So we need to read through the entire first time bucket to collect this metadata.

        time_bucket1 = None
        channel_names = []
        async for row in db_results:
            row_list = list(row)
            if time_bucket1 is None:
                time_bucket1 = row_list[0]
            if not time_bucket1 == row_list[0]:
                break
            channel_names.append(row[1])

        yield ['timestamps', *channel_names]
        n_channels = len(channel_names)

        # Now we want to skip ahead to the portion of the results that we actually care about.
        n_rows_to_skip = start_buffer_time_bucket_count * n_channels
        n_rows_to_take = relevant_time_bucket_count * n_channels
        relevant_results = islice(db_results, n_rows_to_skip, n_rows_to_skip + n_rows_to_take)

        # print(f'************** {time.time() - start_time:.3f}s *** skipped first {n_rows_to_skip} rows')

        for t in range(0, relevant_time_bucket_count):
            time_bucket_result = None
            for ch in range(0, n_channels):
                row = await anext(relevant_results)
                # if t == 0 and ch == 0:
                #     print(f'************** {time.time() - start_time:.3f}s *** retrieved first relevant result')
                if time_bucket_result is None:
                    time_bucket_result = [row[0].astimezone(TZ_NEW_YORK).strftime('%Y-%m-%d %H:%M:%S')]
                time_bucket_result.append(row[2])
            
            yield time_bucket_result

# def match_requested_readings(channel_readings: list[ChannelReadingsListItem], requested_channels: list[str]) -> list[ChannelReadingsListItem]:

#     requested_readings = []
#     if 'all-data' in requested_channels:
#         requested_readings.extend(channel_readings)
#     else:
#         for c in requested_channels:
#             if is_regex(c):
#                 re_results: list[tuple[ChannelReadingsListItem, re.Match[str]]] = [r for r in [(cr, re.fullmatch(c, cr.channel_name)) for cr in channel_readings] if r[1] is not None] # type: ignore
#                 # Sort by the regex capture groups first, numerically if possible
#                 re_results.sort(key=lambda r: (*([int(g) if g.isdigit() else g for g in r[1].groups()]), r[0].channel_name))
#                 matches = [r[0] for r in re_results]
#             else:
#                 matches = list(filter(lambda cr: c == cr.channel_name, channel_readings))
#             requested_readings.extend(matches)
    
#     return requested_readings

async def csv_generator(
    readings_row_generator,
    requested_channels: list[str],
    addl_headers: list[str],
    column_names: list[str],
):
    # readings_metadata is a list of 
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(addl_headers)

    # column_names will be in alphabetical order. We want to re-arrange the columns to match the order in the request.
    sorted_channel_names = []
    if 'all-data' in requested_channels:
        sorted_channel_names = column_names
    else:
        sorted_channel_names.append('timestamps')
        for c in requested_channels:
            if is_regex(c):
                re_results: list[tuple[str, re.Match[str]]] = [r for r in [(name, re.fullmatch(c, name)) for name in column_names] if r[1] is not None] # type: ignore
                # Sort by the regex capture groups first, numerically if possible
                re_results.sort(key=lambda r: (*([int(g) if g.isdigit() else g for g in r[1].groups()]), r[0]))
                matches = [r[0] for r in re_results]
            else:
                matches = list(filter(lambda name: c == name, column_names))
            sorted_channel_names.extend(matches)
    
    writer.writerow(sorted_channel_names)
    # Column 0 is always the timestamp, which is not one of the channels
    sorted_channel_indices = [column_names.index(n) for n in sorted_channel_names]
    
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    # print(f'************** {time.time() - start_time:.3f}s *** written header')

    # Iterate over the stream chunk-by-chunk
    counter = 0
    async for row in readings_row_generator:
        counter += 1
        row_data = list(row)
        sorted_row_data = [row_data[i] for i in sorted_channel_indices]
        writer.writerow(sorted_row_data)

        # Clear the buffer after each 1,000 rows to keep memory footprint flat
        if counter % 1000 == 0:
            yield buffer.getvalue()
            
            buffer.seek(0)
            buffer.truncate(0)

    yield buffer.getvalue()
    # print(f'************** {time.time() - start_time:.3f}s *** written all {counter} rows')



@router.get('/api/v2/installations/{installation_id}/readings.download')
async def get_readings(
    installation_id, 
    query: Annotated[ReadingsDownloadQueryParams, Query()], 
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):

    # start_time = time.time()      
    await verify_data_access(db, username, installation_id, effective_date=query.end)

    time_range_seconds = (query.end - query.start).total_seconds()
    time_step_seconds = query.time_step

    num_points_requested = math.floor(time_range_seconds / time_step_seconds) + 1
    if num_points_requested > settings.max_reading_points:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{num_points_requested:,} points requested exceeds limit of {settings.max_reading_points:,}. Select a shorter time range or larger time step."
        )

    channels = query.channels.split(',')
    str_channels, regexp_channels = determine_query_channels(channels)

    # print(f'************** {time.time() - start_time:.3f}s *** query_readings start')
    readings_row_generator = query_readings(db, query.start, query.end, time_step_seconds, installation_id, str_channels, regexp_channels)
    # print(f'************** {time.time() - start_time:.3f}s *** query_readings end')

    column_names = await anext(readings_row_generator)
    if not column_names:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No data was found for the selected time range."
        )
    # print(f'************** {time.time() - start_time:.3f}s *** channel_names found')

    formatted_start_date = query.start.isoformat()[:16].replace('T', '_').replace(':', '').replace('-', '')
    formatted_end_date = query.end.isoformat()[:16].replace('T', '_')
    installation_alias = installation_id.split('.')[-1]
    filename = f'{installation_alias}_{time_step_seconds}s_{formatted_start_date}-{formatted_end_date}.csv'


    # # Write synchronously (helpful for debugging)
    # rows = [r async for r in readings_row_generator]
    # print(f'************** {time.time() - start_time:.3f}s *** generated all rows')

    # with io.StringIO() as csv_buffer:
    #     csv_buffer.write(f'{filename},{installation_id}\n')
    #     writer = csv.writer(csv_buffer)
    #     writer.writerow(channel_names)
    #     for row in rows:
    #         if row:
    #             writer.writerow(row)

    #     return StreamingResponse(
    #         iter([csv_buffer.getvalue()]),
    #         media_type="text/csv",
    #         headers={"Content-Disposition": f"attachment; filename={filename}"}
    #     )

    return StreamingResponse(
        csv_generator(
            readings_row_generator,
            requested_channels=channels,
            addl_headers=[filename, installation_id],
            column_names=column_names,
        ),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
