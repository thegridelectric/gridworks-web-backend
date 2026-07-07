from datetime import UTC, datetime
from typing import Annotated, Any, List, Self, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec
from api.sema.enums import Gw1MainAutoState, Gw1SystemMode
from api.sema.property_format import SpaceheatName, UtcIso8601Seconds
from api.sema.types import SnapshotSpaceheat, LayoutLite
from api.sema.types.old_versions.layout_lite_011 import LayoutLite011
from api.sema.types.old_versions.layout_lite_012 import LayoutLite012

from ..dependencies import get_current_username, get_db
from ..util import datetime_to_sema

from gw_data.db.models import GNodeSql, InstallationSql, MessageSql, UserSql
from gw_data.db.models.user_installation_role import UserInstallationRoleSql

class InstallationSummary(BaseModel):
    role: str
    g_node_alias: str
    display_name: str
    address: dict[Any, Any]
    # alert_status: dict[Any, Any]
    # commit: str
    system_mode: Gw1SystemMode
    main_auto_state: Gw1MainAutoState | None
    latest_snapshot_time: UtcIso8601Seconds | None
    longest_running_zone_name: SpaceheatName | None
    longest_running_zone_start_time: UtcIso8601Seconds | None



router = APIRouter()
@router.get("/api/v2/installations/*/summaries", response_model=List[InstallationSummary])
async def get_summaries(
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    
    sql = text("""
        SELECT
            g_nodes.alias,
            role,
            installations.display_name,
            address,
            latest_layouts->0 AS latest_layout,
            latest_snapshots->0 AS latest_snapshot,
            heat_calls.name,
            heat_calls.latest_off_time
        FROM gridworks.user_installation_roles
        JOIN gridworks.users on users.id = user_installation_roles.user_id
        JOIN gridworks.installations on installations.id = user_installation_roles.installation_id OR user_installation_roles.installation_id IS NULL
        JOIN gridworks.g_nodes ON g_nodes.id = installations.g_node_id 
        LEFT OUTER JOIN (
            WITH latest_messages_with_type AS (
                SELECT 
                    from_alias,
                    message_type_name,
                    payload,
                    row_number() OVER (
                        PARTITION BY from_alias,message_type_name ORDER BY timestamp DESC
                    ) AS row_number
                FROM gridworks.messages
                WHERE message_type_name in ('snapshot.spaceheat', 'layout.lite')
            )
            SELECT
                from_alias,
                JSONB_AGG(payload) FILTER (WHERE message_type_name = 'snapshot.spaceheat' AND row_number = 1) AS latest_snapshots,
                JSONB_AGG(payload) FILTER (WHERE message_type_name = 'layout.lite' AND row_number = 1) AS latest_layouts
            FROM latest_messages_with_type
            GROUP BY from_alias
        ) AS ordered_messages on ordered_messages.from_alias LIKE (g_nodes.alias || '%')
        LEFT OUTER JOIN (
            SELECT
                *, 
                ROW_NUMBER() OVER (
                    PARTITION BY terminal_asset_alias ORDER by latest_off_time
                ) AS row_number
            FROM (
                SELECT
                    terminal_asset_alias,
                    name,
                    MAX(timestamp) as latest_off_time
                FROM gridworks.readings
                JOIN gridworks.reading_channels on reading_channels.id = readings.channel_id
                WHERE reading_channels.name like '%heat-call%'
                AND timestamp >= NOW() - INTERVAL '24 hours'
                AND value=0
                GROUP BY terminal_asset_alias,name
            )
        ) AS heat_calls on heat_calls.terminal_asset_alias = (g_nodes.alias || '.ta') AND heat_calls.row_number = 1
        WHERE gridworks.users.username = :username
    """)

    db_results = await db.execute(sql, {
        "username": username,
    })

    codec = SemaCodec()
    result = []
    for row in db_results.all():
        [g_node_alias, role, display_name, address, layout_dict, snapshot_dict, longest_running_zone_name, longest_running_zone_time] = row

        layout: InstallationSummaryLayoutLite = cast(InstallationSummaryLayoutLite, codec.from_dict(layout_dict))

        snapshot: SnapshotSpaceheat | None = None
        if snapshot_dict is not None:
            snapshot = cast(SnapshotSpaceheat, codec.from_dict(snapshot_dict))

        result.append(InstallationSummary(
            address=address,
            display_name=display_name,
            g_node_alias=g_node_alias,
            latest_snapshot_time=datetime_to_sema(datetime.fromtimestamp(snapshot.snapshot_time_unix_ms / 1000, tz=UTC)) if snapshot is not None else None,
            longest_running_zone_name=longest_running_zone_name,
            longest_running_zone_start_time = datetime_to_sema(cast(datetime, longest_running_zone_time)) if longest_running_zone_time is not None else None,
            role=role,
            system_mode=layout.system_mode,
            main_auto_state=find_main_auto_state(snapshot) if snapshot is not None else None
        ))

    return result    

type InstallationSummaryLayoutLite = LayoutLite | LayoutLite012 | LayoutLite011

def find_main_auto_state(snapshot: SnapshotSpaceheat) -> Gw1MainAutoState | None:
    topState = next((x.state for x in snapshot.latest_state_list if x.state_enum == 'top.state'), None)
    if topState is None:
        return None
    
    mainAutoState = next((x.state for x in snapshot.latest_state_list if x.state_enum == 'gw1.main.auto.state'), None)
    return next((x for x in Gw1MainAutoState if x.value == mainAutoState), None)
    
