from datetime import UTC, datetime
from typing import List, cast
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.base import snake_to_pascal
from api.sema.codec import SemaCodec
from api.sema.enums import Gw1MainAutoState, Gw1SystemMode
from api.sema.property_format import SpaceheatName, UtcIso8601Seconds
from api.sema.types import SnapshotSpaceheat, LayoutLite
from api.sema.types.old_versions.layout_lite_011 import LayoutLite011

from ..dependencies import get_current_username, get_db
from ..util import datetime_to_sema

type InstallationSummaryLayoutLite = LayoutLite | LayoutLite011


class InstallationSummary(BaseModel):
    """One house the login may see. The house is keyed by its TerminalAsset
    GNodeId, the same id the roles table and the customer store use; the
    alias is the house's, without the ".ta". Nothing about a person is here.
    The layout and system mode are None for a house with no layouts row yet."""
    role: str
    ta_g_node_id: uuid.UUID
    g_node_alias: str
    display_name: str | None
    system_mode: Gw1SystemMode | None
    main_auto_state: Gw1MainAutoState | None
    latest_snapshot_time: UtcIso8601Seconds | None
    longest_running_zone_name: SpaceheatName | None
    longest_running_zone_start_time: UtcIso8601Seconds | None
    layout_lite: InstallationSummaryLayoutLite | None

    model_config = ConfigDict(
        alias_generator=snake_to_pascal,
        frozen=True,
        populate_by_name=True,
        extra="forbid",
    )

router = APIRouter()
@router.get("/api/v2/installations/*/summaries", response_model=List[InstallationSummary])
async def get_summaries(
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    # The house list is the TerminalAsset rows of the g_nodes projection; a
    # role with a null ta_g_node_id sees every house. The TA alias is the
    # house alias plus ".ta"; the scada and the LTN send under the house alias.
    # The layout is the house's row in the layouts projection, read by key;
    # the snapshot is the newest in the last day, since one arrives every few
    # minutes and an older one says the house is not reporting.
    sql = text("""
        SELECT
            ta.id,
            left(ta.alias, -3) AS house_alias,
            role,
            ta.display_name,
            layouts.payload AS layout,
            latest_snapshot.payload AS snapshot,
            heat_calls.name,
            heat_calls.latest_off_time
        FROM gridworks.user_installation_roles
        JOIN gridworks.users on users.id = user_installation_roles.user_id
        JOIN gridworks.g_nodes ta
            ON (ta.id = user_installation_roles.ta_g_node_id OR user_installation_roles.ta_g_node_id IS NULL)
            AND ta.g_node_class = 'TerminalAsset'
        LEFT OUTER JOIN gridworks.layouts ON layouts.ta_g_node_id = ta.id
        LEFT OUTER JOIN (
            SELECT from_alias, payload
            FROM (
                SELECT
                    from_alias,
                    payload,
                    row_number() OVER (PARTITION BY from_alias ORDER BY timestamp DESC) AS row_number
                FROM gridworks.messages
                WHERE message_type_name = 'snapshot.spaceheat'
                AND timestamp >= NOW() - INTERVAL '24 hours'
            ) AS recent_snapshots
            WHERE row_number = 1
        ) AS latest_snapshot on latest_snapshot.from_alias LIKE (left(ta.alias, -3) || '%')
        LEFT OUTER JOIN (
            SELECT
                *, 
                ROW_NUMBER() OVER (
                    PARTITION BY terminal_asset_alias ORDER by latest_off_time
                ) AS row_number
            FROM (
                WITH latest_readings AS (
                    SELECT
                        reading_channels.id,
                        value,
                        ROW_NUMBER() OVER (PARTITION BY reading_channels.id ORDER BY timestamp DESC) AS row_number
                    FROM gridworks.readings
                    JOIN gridworks.reading_channels on reading_channels.id = readings.channel_id
                    WHERE reading_channels.name like '%heat-call%'
                    AND timestamp >= NOW() - INTERVAL '24 hours'
                )
                SELECT id, value
                FROM latest_readings
                WHERE row_number = 1 and value = 1
            ) AS active_heat_calls
            JOIN (
                SELECT
        			reading_channels.id,
                    terminal_asset_alias,
                    name,
                    MAX(timestamp) as latest_off_time
                FROM gridworks.readings
                JOIN gridworks.reading_channels on reading_channels.id = readings.channel_id
                WHERE reading_channels.name like '%heat-call%'
                AND timestamp >= NOW() - INTERVAL '24 hours'
                AND value=0
                GROUP BY reading_channels.id,terminal_asset_alias
            ) AS last_inactive_times
            ON active_heat_calls.id = last_inactive_times.id
        ) AS heat_calls on heat_calls.terminal_asset_alias = ta.alias AND heat_calls.row_number = 1
        WHERE gridworks.users.username = :username
    """)

    db_results = await db.execute(sql, {
        "username": username,
    })

    codec = SemaCodec()
    result = []
    for row in db_results.all():
        [
            ta_g_node_id,
            g_node_alias,
            role,
            display_name,
            layout_dict,
            snapshot_dict,
            longest_running_zone_name,
            longest_running_zone_time,
        ] = row

        layout: InstallationSummaryLayoutLite | None = None
        if layout_dict is not None:
            layout = cast(InstallationSummaryLayoutLite, codec.from_dict(layout_dict))

        snapshot: SnapshotSpaceheat | None = None
        if snapshot_dict is not None:
            snapshot = cast(SnapshotSpaceheat, codec.from_dict(snapshot_dict))

        result.append(InstallationSummary(
            role=role,
            ta_g_node_id=ta_g_node_id,
            g_node_alias=g_node_alias,
            display_name=display_name,
            system_mode=layout.system_mode if layout is not None else None,
            main_auto_state=find_main_auto_state(snapshot) if snapshot is not None else None,
            latest_snapshot_time=datetime_to_sema(datetime.fromtimestamp(snapshot.snapshot_time_unix_ms / 1000, tz=UTC)) if snapshot is not None else None,
            longest_running_zone_name=longest_running_zone_name,
            longest_running_zone_start_time = datetime_to_sema(cast(datetime, longest_running_zone_time)) if longest_running_zone_time is not None else None,
            layout_lite=layout,
        ))

    return result    


def find_main_auto_state(snapshot: SnapshotSpaceheat) -> Gw1MainAutoState | None:
    topState = next((x.state for x in snapshot.latest_state_list if x.state_enum == 'top.state'), None)
    if topState is None:
        return None
    
    mainAutoState = next((x.state for x in snapshot.latest_state_list if x.state_enum == 'gw1.main.auto.state'), None)
    return next((x for x in Gw1MainAutoState if x.value == mainAutoState), None)
    
