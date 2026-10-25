from datetime import datetime, timedelta
import io
import math
from typing import Annotated, Self
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy import Float, String, Uuid, column, func, or_, select, text, values
from sqlalchemy.ext.asyncio import AsyncSession

from gw_data.db.models import (
    MessageSql,
    ReadingChannelSql,
    ReadingSql,
)

from api.config import Settings
from api.sema.property_format import UtcIso8601MillisFormat
from api.sema.types import (
    ChannelReadingsListItem,
    OperatingStateSequence,
    SyncedReadingsBundle,
)
from ..readings_units import (
    UNIT_VOCABULARIES,
    ChannelUnit,
    UnitConflict,
    column_values,
    lineages,
    newest_row,
    numeric_columns,
    select_columns,
    state_value,
)
from ..shared_queries import verify_data_access
from ..util import datetime_to_sema

from ..dependencies import get_current_username, get_db, get_settings

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

async def query_readings_with_times(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    time_step_seconds: int,
    installation_id: str,
    requested_channels: list[str],
) -> tuple[list[ChannelReadingsListItem], list[datetime]]:
    """One series per requested channel lineage, in the request's order,
    each in its newest row's unit with every older row's readings converted
    into it (`readings_units`), bucketed to `time_step_seconds` as the
    time-weighted average. A lineage whose rows do not convert into one
    another raises UnitConflict."""
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

    # Every row of the house with a reading in the queried range: the lineages are knit from all of them, and the
    # request then picks series, so a series asked for by one of its names comes whole.
    reading_channels_query = select(
        ReadingChannelSql.id, ReadingChannelSql.name, ReadingChannelSql.unit_type, ReadingChannelSql.unit,
        ReadingChannelSql.deactivated_date, ReadingChannelSql.channel_id,
    ).distinct().join(ReadingSql).filter(
        ReadingSql.timestamp >= db_query_start,
        ReadingSql.timestamp <= db_query_end,
        ReadingChannelSql.terminal_asset_alias == installation_id + ".ta",
    )
    reading_channels_result = await db.execute(reading_channels_query)
    columns = numeric_columns(
        select_columns(lineages([ChannelUnit(*row) for row in reading_channels_result.all()]), requested_channels)
    )
    if not columns:
        return [], []
    cell_rules = column_values(columns)

    # Each row of a series, with the affine map that puts its stored values in the series' unit. The readings join
    # this table, so a reading is converted before it is averaged and the rows of one series average together.
    series_rules = values(
        column("row_id", Uuid), column("series", String), column("slope", Float), column("intercept", Float),
        name="series_rules",
    ).data([
        (row_id, rule.column, rule.affine.slope, rule.affine.intercept)
        for row_id, rule in cell_rules.items()
        if rule.affine is not None
    ])

    query_interval = text(f"INTERVAL '{time_step_seconds} seconds'")

    # The innermost query gets the time-weighted interval data for the selected time range and series
    #
    # SELECT
    # 	series_rules.series AS channel_name,
    # 	time_bucket(INTERVAL '30 seconds', readings.timestamp) AS time_bucket,
    # 	time_weight('LOCF', readings.timestamp, readings.value * series_rules.slope + series_rules.intercept) AS time_weight
    # FROM readings
    # JOIN (VALUES (...)) AS series_rules ON series_rules.row_id = readings.channel_id
    # WHERE
    # 	readings.timestamp >= '2026-01-02T00:00:00'
    # 	AND readings.timestamp <= '2026-01-02T00:05:00'
    # GROUP BY time_bucket, series_rules.series
    # ORDER BY series_rules.series, time_bucket

    time_weight_query = (
        select(
            series_rules.c.series.label("channel_name"),
            func.time_bucket(query_interval, ReadingSql.timestamp).label("time_bucket"),
            func.time_weight(
                "LOCF", ReadingSql.timestamp, ReadingSql.value * series_rules.c.slope + series_rules.c.intercept
            ).label("time_weight"),
        )
        .select_from(ReadingSql)
        .join(series_rules, series_rules.c.row_id == ReadingSql.channel_id)
        .filter(
            ReadingSql.timestamp >= db_query_start,
            ReadingSql.timestamp <= db_query_end,
        )
        .group_by(text("time_bucket"), series_rules.c.series)
        .order_by(series_rules.c.series, text("time_bucket"))
        .subquery()
    )

    # The next query calculates the time-weighted average for the data
    #
    # SELECT
    # 	anon_2.channel_name AS channel_name,
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
    # GROUP BY anon_1.channel_name, time_bucket_gapfilled

    gapfilled_query = (
        select(
            interpolated_avg_query.c.channel_name,
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
            text("time_bucket_gapfilled"),
        )
        .subquery()
    )

    # The final query coalesces the values -- using the time-weighted average if it's available for a bucket,
    # otherwise the most recent reading value.
    final_query = select(
        gapfilled_query.c.channel_name,
        gapfilled_query.c.time_bucket_gapfilled,
        func.coalesce(
            gapfilled_query.c.avg_value, gapfilled_query.c.last_reading_value
        ).label("value"),
    ).order_by(
        gapfilled_query.c.channel_name,
        gapfilled_query.c.time_bucket_gapfilled,
    )

    db_result = await db.execute(final_query)
    buckets_by_series: dict[str, list[tuple[datetime, float | None]]] = {}
    for name, bucket, value in db_result.all():
        if start <= bucket <= end:
            buckets_by_series.setdefault(name, []).append((bucket, value))

    channel_readings: list[ChannelReadingsListItem] = []
    times: list[datetime] = []
    for col in columns:
        buckets = buckets_by_series.get(col.name)
        if not buckets:
            continue
        if not times:
            times = [bucket for bucket, _ in buckets]
        unit_row = newest_row(col.rows)
        channel_readings.append(
            ChannelReadingsListItem(
                channel_name=col.name,
                unit=unit_row.unit,
                unit_type=unit_row.unit_type,
                value_list=[None if value is None else round(value) for _, value in buckets],
            )
        )

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

async def query_operating_state_sequences(
    db: AsyncSession, start: datetime, end: datetime, installation_id: str
) -> list[OperatingStateSequence]:
    """One sequence per state channel name: the state's symbol at each change
    inside the window. A state row is a `reading_channels` row whose unit type
    is an enum (outside the unit vocabularies) and whose unit is the enum
    version; a stored position resolves through that pair, and a row stamped
    `Enum` through the newest vendored version (`readings_units.state_value`).
    The rows of one name (an enum version change keeps the name) feed one
    sequence, and a change is detected on the symbol, since positions
    renumber across versions. A row whose enum the snapshot does not vendor,
    and a position outside its enum, give no points: there is no symbol to
    send. Times are to the millisecond, as the scada reports them: a machine
    can change state twice in one second (a boot), and the word's times are
    strictly increasing; a second change at the very same millisecond
    replaces the first."""
    state_rows_query = select(
        ReadingChannelSql.id, ReadingChannelSql.name, ReadingChannelSql.unit_type, ReadingChannelSql.unit,
        ReadingChannelSql.deactivated_date, ReadingChannelSql.channel_id,
    ).distinct().join(ReadingSql).filter(
        ReadingSql.timestamp >= start,
        ReadingSql.timestamp <= end,
        ReadingChannelSql.terminal_asset_alias == installation_id + ".ta",
        ReadingChannelSql.unit_type.not_in(UNIT_VOCABULARIES),
    )
    symbol_rules = {}
    for row in (ChannelUnit(*r) for r in (await db.execute(state_rows_query)).all()):
        try:
            symbol_rules[row.id] = state_value(row, row.name)
        except UnitConflict:
            continue
    if not symbol_rules:
        return []

    readings_query = select(ReadingSql.channel_id, ReadingSql.timestamp, ReadingSql.value).where(
        ReadingSql.channel_id.in_(symbol_rules),
        ReadingSql.timestamp >= start,
        ReadingSql.timestamp <= end,
    ).order_by(ReadingSql.timestamp)

    state_sequences: dict[str, OperatingStateSequence] = {}
    for row_id, timestamp, value in (await db.execute(readings_query)).all():
        rule = symbol_rules[row_id]
        symbol = rule.convert(value)
        if not isinstance(symbol, str):
            continue
        at = UtcIso8601MillisFormat.from_datetime(timestamp)
        sequence = state_sequences.get(rule.column)
        if sequence is None:
            sequence = state_sequences[rule.column] = OperatingStateSequence(
                channel_name=rule.column, timestamp_list=[], value_list=[]
            )
        elif sequence.timestamp_list[-1] == at:
            sequence.value_list[-1] = symbol
            continue
        elif sequence.value_list[-1] == symbol:
            continue
        sequence.timestamp_list.append(at)
        sequence.value_list.append(symbol)

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

    try:
        channel_readings, times = await query_readings_with_times(
            db, query.start, query.end, time_step_seconds, installation_id, query.channels.split(',')
        )
    except UnitConflict as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e

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
