from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec

from ..dependencies import get_current_username, get_db

from gw_data.db.models import GNodeSql, InstallationSql, MessageSql, UserSql
from gw_data.db.models.user_installation_role import UserInstallationRoleSql

router = APIRouter()
@router.get("/api/v2/installations/{installation_id_param}/summaries")
async def get_summaries(
    installation_id_param: str,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    
    sql = text("""
        SELECT
            g_nodes.alias,
            role,
            installations.display_name,
            address,
            payload,
            heat_calls.name,
            heat_calls.latest_off_time
        FROM gridworks.user_installation_roles
        JOIN gridworks.users on users.id = user_installation_roles.user_id
        JOIN gridworks.installations on installations.id = user_installation_roles.installation_id OR user_installation_roles.installation_id IS NULL
        JOIN gridworks.g_nodes ON g_nodes.id = installations.g_node_id 
        LEFT OUTER JOIN (
            SELECT 
                from_alias,
                payload,
                row_number() OVER (
                    PARTITION BY from_alias,message_type_name ORDER BY timestamp DESC
                ) AS row_number
            FROM gridworks.messages
            WHERE message_type_name = 'snapshot.spaceheat'
        ) AS ordered_messages ON (
               ordered_messages.from_alias LIKE (g_nodes.alias || '%') AND 
               ordered_messages.row_number = 1 AND
               (role = 'admin' OR role = 'owner')
            )
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
        ) AS heat_calls ON (
               heat_calls.terminal_asset_alias = (g_nodes.alias || '.ta') AND 
               heat_calls.row_number = 1 AND
               (role = 'admin' OR role = 'owner')
            )
        WHERE gridworks.users.username = :username
    """)

    db_results = await db.execute(sql, {
        "username": username,
    })

    
    all_rows = db_results.all()
    return [list(x) for x in all_rows]
