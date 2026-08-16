from datetime import datetime, timedelta
import io
import math
import re
from typing import Annotated, Self, Tuple
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession



from gw_data.db.models import (
    MessageSql,
    ReadingChannelSql,
    ReadingSql,
)

from api.config import Settings
from api.sema.enums.gw_str_enum import SemaEnum
from api.sema import enums as sema_enums
from api.sema.types import (
    ChannelReadingsListItem,
    OperatingStateSequence,
    SyncedReadingsBundle,
)
from ..shared_queries import verify_data_access
from ..util import datetime_to_sema

from ..dependencies import get_current_username, get_db, get_settings

SEMA_ENUM_LOOKUP: dict[str, SemaEnum] = {
    enum_class.enum_name(): enum_class
    for enum_class in [getattr(sema_enums, type_name) for type_name in sema_enums.__all__]
}

router = APIRouter()

class ReadingsQueryParams(BaseModel):
    start: datetime
    end: datetime
    time_step: int | None = None
    channels: str = Field('')
    dl: bool | None = None

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end_time must be after start_time")
        return self

DEFAULT_TIME_STEPS = [1,5,30,60,300,1800]

whitewire_pwr_threshold_default = 20
whitewire_pwr_threshold_overrides = {"hw1.isone.me.versant.keene.beech": 100, "hw1.isone.me.versant.keene.elm": 1}

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

async def query_readings_with_times(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    time_step_seconds: int,
    installation_id: str,
    str_channels: set[str] | None,
    regexp_channels: set[str] | None,
) -> tuple[list[ChannelReadingsListItem], list[datetime]]:
    # To get an accurate and complete set of time-averaged data for the requested time range,
    # our query needs to include the last value from before our time range begins.
    # Otherwise, data will be missing for any of our time buckets that end before the timestamp of our first value.
    # Additionally, the first time bucket that actually does contain a value will not be able to compute an accurate
    # average value, since it won't know its starting value.
    #
    # We have no good way to know how far back to search, so we just go 30 minutes and hope it's enough.
    # Then we need to skip this many records when returning the results
    start_buffer_step_count = 60 * 30 // time_step_seconds
    start_buffer_s = start_buffer_step_count * time_step_seconds
    db_query_start = start - timedelta(seconds=start_buffer_s)

    # Additionally, we need to query for a full time step after our time range so that we can calculate the average value
    # of the time bucket that begins at the requested end time.
    db_query_end = end + timedelta(seconds=time_step_seconds)

    query_interval = text(f"INTERVAL '{time_step_seconds} seconds'")

    # The innermost query gets the time-weighted interval data for the selected time range, terminal asset, and channels
    #
    # SELECT
    # 	reading_channels.name AS channel_name,
    # 	reading_channels.unit AS channel_unit,
    # 	reading_channels.unit_type AS channel_unit_type,
    # 	time_bucket(INTERVAL '30 seconds', readings.timestamp) AS time_bucket,
    # 	time_weight('LOCF', readings.timestamp, readings.value) AS time_weight
    # FROM readings
    # JOIN reading_channels ON reading_channels.id = readings.channel_id
    # WHERE
    # 	readings.timestamp >= '2026-01-02T00:00:00'
    # 	AND readings.timestamp <= '2026-01-02T00:05:00'
    # 	AND reading_channels.terminal_asset_alias = 'hw1.isone.me.versant.keene.beech.ta'
    # 	AND reading_channels.name IN ('hp-ewt')
    # GROUP BY time_bucket, reading_channels.name, reading_channels.unit, reading_channels.unit_type
    # ORDER BY reading_channels.name, time_bucket

    channel_filter = text('1=1') if str_channels is None or regexp_channels is None else or_(
        ReadingChannelSql.name.in_(str_channels),
        *map(lambda x: ReadingChannelSql.name.regexp_match(x), regexp_channels),
    ) 

    time_weight_query = (
        select(
            ReadingChannelSql.name.label("channel_name"),
            ReadingChannelSql.unit.label("channel_unit"),
            ReadingChannelSql.unit_type.label("channel_unit_type"),
            func.time_bucket(query_interval, ReadingSql.timestamp).label("time_bucket"),
            func.time_weight("LOCF", ReadingSql.timestamp, ReadingSql.value).label(
                "time_weight"
            ),
        )
        .join(ReadingChannelSql)
        .filter(
            ReadingSql.timestamp >= db_query_start,
            ReadingSql.timestamp <= db_query_end,
            ReadingChannelSql.terminal_asset_alias == installation_id + ".ta",
            channel_filter
        )
        .group_by(
            text("time_bucket"),
            ReadingChannelSql.name,
            ReadingChannelSql.unit,
            ReadingChannelSql.unit_type,
        )
        .order_by(ReadingChannelSql.name, text("time_bucket"))
        .subquery()
    )

    # The next query calculates the time-weighted average for the data
    #
    # SELECT
    # 	anon_2.channel_name AS channel_name,
    # 	anon_2.channel_unit AS channel_unit,
    # 	anon_2.channel_unit_type AS channel_unit_type,
    # 	anon_2.time_bucket AS time_bucket,
    # 	locf(last_val(anon_2.time_weight)) AS last_reading_value,
    # 	interpolated_average(
    # 		anon_2.time_weight,
    # 		time_bucket,
    # 		INTERVAL '30 seconds',
    # 		lag(anon_2.time_weight) OVER (PARTITION BY channel_name ORDER BY time_bucket),
    # 		lead(anon_2.time_weight) OVER (PARTITION BY channel_name ORDER BY time_bucket)
    # 	) AS avg_value
    # FROM (
    #   -- time_weight_query
    # ) AS anon_2

    interpolated_avg_query = select(
        time_weight_query.c.channel_name.label("channel_name"),
        time_weight_query.c.channel_unit.label("channel_unit"),
        time_weight_query.c.channel_unit_type.label("channel_unit_type"),
        time_weight_query.c.time_bucket.label("time_bucket"),
        func.locf(func.last_val(time_weight_query.c.time_weight)).label(
            "last_reading_value"
        ),
        func.interpolated_average(
            time_weight_query.c.time_weight,
            text("time_bucket"),
            query_interval,
            func.lag(time_weight_query.c.time_weight).over(
                partition_by=text("channel_name"), order_by=text("time_bucket")
            ),
            func.lead(time_weight_query.c.time_weight).over(
                partition_by=text("channel_name"), order_by=text("time_bucket")
            ),
        ).label("avg_value"),
    ).subquery()

    # The next query fills in gaps where there was no data
    #
    # SELECT
    # 	anon_1.channel_name,
    # 	anon_1.channel_unit,
    # 	anon_1.channel_unit_type,
    # 	time_bucket_gapfill(INTERVAL '30 seconds', anon_1.time_bucket) AS time_bucket_gapfilled,
    #   max(anon_1.avg_value) as avg_value,
    # 	locf(max(anon_1.avg_value)) AS locf_value
    # FROM (
    #   -- interpolated_avg_query
    # ) AS anon_1
    # WHERE
    # 	anon_1.avg_value IS NOT NULL
    # 	AND anon_1.time_bucket >= '2026-01-02T00:00:00'
    # 	AND anon_1.time_bucket <= '2026-01-02T00:05:00'
    # GROUP BY
    # 	anon_1.channel_name, anon_1.channel_unit, anon_1.channel_unit_type,
    # 	time_bucket_gapfilled

    gapfilled_query = (
        select(
            interpolated_avg_query.c.channel_name,
            interpolated_avg_query.c.channel_unit,
            interpolated_avg_query.c.channel_unit_type,
            func.time_bucket_gapfill(
                query_interval, interpolated_avg_query.c.time_bucket
            ).label("time_bucket_gapfilled"),
            func.max(interpolated_avg_query.c.avg_value).label("avg_value"),
            func.locf(func.max(interpolated_avg_query.c.last_reading_value)).label(
                "last_reading_value"
            ),
        )
        .where(
            interpolated_avg_query.c.avg_value.is_not(None),
            interpolated_avg_query.c.time_bucket >= db_query_start,
            interpolated_avg_query.c.time_bucket <= db_query_end,
        )
        .group_by(
            interpolated_avg_query.c.channel_name,
            interpolated_avg_query.c.channel_unit,
            interpolated_avg_query.c.channel_unit_type,
            text("time_bucket_gapfilled"),
        )
    )

    # The final query coalesces the values -- using the time-weighted average if it's available for a bucket,
    # otherwise the most recent reading value.
    final_query = select(
        gapfilled_query.c.channel_name,
        gapfilled_query.c.channel_unit,
        gapfilled_query.c.channel_unit_type,
        gapfilled_query.c.time_bucket_gapfilled,
        func.coalesce(
            gapfilled_query.c.avg_value, gapfilled_query.c.last_reading_value
        ).label("value"),
    ).order_by(
        gapfilled_query.c.channel_name,
        gapfilled_query.c.time_bucket_gapfilled,
    )

    db_result = await db.execute(final_query)
    db_result_rows = db_result.all()

    channel_readings: list[ChannelReadingsListItem] = []
    times: list[datetime] = []
    current_key: tuple[str, str, str] | None = None
    current_rows: list[tuple] = []

    def append_current_channel() -> None:
        nonlocal times
        if not current_rows:
            return
        user_rows = [row for row in current_rows if start <= row[3] <= end]
        if not user_rows:
            return
        if not times:
            times = [row[3] for row in user_rows]
        channel_readings.append(
            ChannelReadingsListItem(
                channel_name=current_rows[0][0],
                unit=current_rows[0][1],
                unit_type=current_rows[0][2],
                value_list=[
                    None if row[4] is None else round(row[4])
                    for row in user_rows
                ],
            )
        )

    for row in db_result_rows:
        key = (row[0], row[1], row[2])
        if key != current_key:
            append_current_channel()
            current_key = key
            current_rows = [row]
        else:
            current_rows.append(row)
    append_current_channel()

    return channel_readings, times


async def query_late_persistence(db: AsyncSession, start: datetime, end: datetime, installation_id: str) -> list[list[str]]:

    # select timestamp, is_delayed from (
    # 	select timestamp, is_delayed, is_delayed <> LAG(is_delayed) OVER (ORDER BY timestamp) as is_delay_changed
    # 	from (
    # 		select timestamp, persisted_at - created_at > '1 minute' as is_delayed
    # 		from messages
    # 		where from_alias like '%spruce%'
    # 	)
    # )
    # where (is_delay_changed IS NULL OR is_delay_changed)
    # order by timestamp

    is_delayed_query = select(
        MessageSql.timestamp.label('timestamp'),
        ((MessageSql.persisted_at - MessageSql.created_at) > text("INTERVAL '5 minutes'")).label('is_delayed')
    ).where(
        MessageSql.from_alias == installation_id + ".scada",
        MessageSql.message_type_name == 'report.event',
        MessageSql.timestamp >= start,
        MessageSql.timestamp <= end
    ).subquery()

    is_delayed_changed_query = select(
        is_delayed_query.c.timestamp,
        is_delayed_query.c.is_delayed,
        (is_delayed_query.c.is_delayed != func.lag(is_delayed_query.c.is_delayed).over(order_by=text('timestamp'))).label('is_delayed_changed')
    ).subquery()

    changelist_query = select(
        is_delayed_changed_query.c.timestamp,
        is_delayed_changed_query.c.is_delayed
    ).where(
        or_(
            is_delayed_changed_query.c.is_delayed_changed.is_(None),
            is_delayed_changed_query.c.is_delayed_changed
        )
    )

    db_result = await db.execute(changelist_query)
    db_result_rows = db_result.all()

    result: list[list[str]] = []
    delay_start = None
    for row in db_result_rows:
        [timestamp, is_delayed] = row
        if is_delayed:
            delay_start = timestamp
        elif delay_start is not None:
            result.append([datetime_to_sema(delay_start), datetime_to_sema(timestamp)])
            delay_start = None

    if delay_start is not None:
        result.append([datetime_to_sema(delay_start), datetime_to_sema(end)])

    return result

async def query_operating_state_sequences(db: AsyncSession, start, end, installation_id):
    # SELECT * FROM (
    #     SELECT name, timestamp, value, value - LAG(value) OVER (PARTITION BY name ORDER BY timestamp) as diff
    #     FROM readings r
    #     JOIN reading_channels rc on rc.id = r.channel_id
    #     WHERE 
    #         rc.terminal_asset_alias like '%beech%'
    #  		  AND rc.unit='Enum'
    #         AND timestamp > '2026-04-20'
    #         AND timestamp < '2026-04-28'
    # )
    # WHERE 
    #     (diff IS NULL OR diff != 0)
    # ORDER BY timestamp;
    state_diff_query = select(
        ReadingChannelSql.name.label('name'),
        ReadingChannelSql.unit_type.label('enum_type_name'),
        ReadingSql.timestamp.label('timestamp'),
        ReadingSql.value.label('value'),
        (ReadingSql.value - func.lag(ReadingSql.value).over(partition_by=text('name'), order_by=text('timestamp'))).label('diff')
    ).join(ReadingChannelSql).where(
        ReadingChannelSql.terminal_asset_alias == installation_id + '.ta',
        ReadingChannelSql.unit == 'Enum',
        ReadingSql.timestamp >= start,
        ReadingSql.timestamp <= end,
    ).subquery()

    is_diff_query = select(
        state_diff_query.c.name,
        state_diff_query.c.enum_type_name,
        state_diff_query.c.timestamp,
        state_diff_query.c.value,
    ).where(
        or_(
            state_diff_query.c.diff.is_(None),
            state_diff_query.c.diff != 0
        )
    ).order_by(state_diff_query.c.timestamp)

    db_result = (await db.execute(is_diff_query)).all()
    
    state_sequences: dict[str, OperatingStateSequence] = {}
    for row in db_result:
        [name, enum_type_name, timestamp, value] = row
        
        if name not in state_sequences:
            state_sequences[name] = OperatingStateSequence(
                channel_name=name,
                timestamp_list=[],
                value_list=[]
            )

        value_str = str(value)
        enum_type = SEMA_ENUM_LOOKUP.get(enum_type_name)
        if enum_type is not None:
            value_str = enum_type.values()[value]
        state_sequences[name].timestamp_list.append(datetime_to_sema(timestamp))
        state_sequences[name].value_list.append(value_str)
        
    return list(state_sequences.values())

TZ_NEW_YORK = ZoneInfo('America/New_York')

def write_readings_to_csv(csv_buffer: io.StringIO, times: list[datetime], readings: list[ChannelReadingsListItem]):
    csv_buffer.write('timestamps')
    for r in readings:
        csv_buffer.write(f',{r.channel_name}')    
    csv_buffer.write('\n')

    for i in range(0, len(times)):
        csv_buffer.write(times[i].astimezone(TZ_NEW_YORK).strftime('%Y-%m-%d %H:%M:%S'))
        for r in readings:
            val = r.value_list[i]
            val_str = '' if val is None else str(val)
            csv_buffer.write(f',{val_str}')
        csv_buffer.write('\n')

def match_requested_readings(channel_readings: list[ChannelReadingsListItem], requested_channels: list[str]) -> list[ChannelReadingsListItem]:

    requested_readings = []
    if 'all-data' in requested_channels:
        requested_readings.extend(channel_readings)
    else:
        for c in requested_channels:
            if is_regex(c):
                re_results: list[tuple[ChannelReadingsListItem, re.Match[str]]] = [r for r in [(cr, re.fullmatch(c, cr.channel_name)) for cr in channel_readings] if r[1] is not None] # type: ignore
                # Sort by the regex capture groups first, numerically if possible
                re_results.sort(key=lambda r: (*([int(g) if g.isdigit() else g for g in r[1].groups()]), r[0].channel_name))
                matches = [r[0] for r in re_results]
            else:
                matches = list(filter(lambda cr: c == cr.channel_name, channel_readings))
            requested_readings.extend(matches)
    
    return requested_readings

@router.get('/api/v2/installations/{installation_id}/synced.readings.bundle')
async def get_readings(
    installation_id, 
    query: Annotated[ReadingsQueryParams, Query()], 
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    
    await verify_data_access(db, username, installation_id, effective_date=query.end)

    time_range_seconds = (query.end - query.start).total_seconds()
    time_step_seconds = query.time_step if query.time_step else next(i for i in DEFAULT_TIME_STEPS if i >= time_range_seconds / settings.max_reading_points)

    num_points_requested = math.floor(time_range_seconds / time_step_seconds) + 1
    if num_points_requested > settings.max_reading_points:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{num_points_requested:,} points requested exceeds limit of {settings.max_reading_points:,}. Select a shorter time range or larger time step."
        )

    channels = query.channels.split(',')
    str_channels, regexp_channels = determine_query_channels(channels)

    channel_readings, times = await query_readings_with_times(db, query.start, query.end, time_step_seconds, installation_id, str_channels, regexp_channels)

    channel_readings = match_requested_readings(channel_readings, channels)

    if query.dl:
        formatted_start_date = query.start.isoformat()[:16].replace('T', '_').replace(':', '').replace('-', '')
        formatted_end_date = query.end.isoformat()[:16].replace('T', '_')
        installation_alias = installation_id.split('.')[-1]
        filename = f'{installation_alias}_{time_step_seconds}s_{formatted_start_date}-{formatted_end_date}.csv'
        with io.StringIO() as csv_buffer:
            csv_buffer.write(f'{filename},{installation_id}\n')
            write_readings_to_csv(csv_buffer, times, channel_readings)
            return StreamingResponse(
                iter([csv_buffer.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )


    result = SyncedReadingsBundle(
        about_g_node_alias=installation_id + ".ta",
        start_timestamp=datetime_to_sema(query.start),
        end_timestamp=datetime_to_sema(query.end),
        timestamp_list=[datetime_to_sema(t) for t in times],
        channel_readings_list=channel_readings,
        late_persistence_time_period_list=await query_late_persistence(db, query.start, query.end, installation_id),
        operating_state_sequence_list=await query_operating_state_sequences(db, query.start, query.end, installation_id)
    )

    return result
