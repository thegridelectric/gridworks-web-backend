import logging
import time
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import Settings
from api.sema.codec import SemaCodec
from api.sema.enums import GwCommitStanding, GwDashboardRole
from api.sema.types import GwHouseOperatingStatus, GwHouseSummary, LayoutLite, SnapshotSpaceheat
from api.sema.types.old_versions.layout_lite_011 import LayoutLite011
from api.sema.types.old_versions.layout_lite_012 import LayoutLite012

from ..commit_standing import CommitStandings
from ..dependencies import get_current_username, get_db, get_settings
from ..util import datetime_to_sema

logger = logging.getLogger(__name__)

router = APIRouter()

commit_standings: CommitStandings | None = None


def get_commit_standings(settings: Settings = Depends(get_settings)) -> CommitStandings:
    """The process-wide resolver, so its cache outlives one request."""
    global commit_standings
    if commit_standings is None:
        commit_standings = CommitStandings(settings.scada_repo)
    return commit_standings


@router.get("/api/v2/installations/*/summaries", response_model=None)
async def get_summaries(
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
    standings: CommitStandings = Depends(get_commit_standings),
) -> list[dict]:
    """The houses the login may see, one `gw.house.summary` instance each,
    in the word's dict form."""
    # The house list is the TerminalAsset rows of the g_nodes projection; a
    # role with a null ta_g_node_id sees every house. The TA alias is the
    # house alias plus ".ta"; the scada and the LTN send under the house alias.
    # The layout and the operating status are the house's rows in the layouts
    # and house_operating_status projections, read by key; the snapshot is the
    # newest in the last day, since one arrives every few minutes and an older
    # one says the house is not reporting. The scada's commit is the newest
    # gw.scada.commit its scada announced; a scada that never announced one
    # (legacy code) leaves it null.
    sql = text("""
        SELECT
            ta.id,
            left(ta.alias, -3) AS house_alias,
            role,
            ta.display_name,
            layouts.payload AS layout,
            house_operating_status.payload AS operating_status,
            latest_snapshot.payload AS snapshot,
            heat_calls.name,
            heat_calls.latest_off_time,
            scada_commit.payload->>'GitCommit' AS scada_commit
        FROM gridworks.user_installation_roles
        JOIN gridworks.users on users.id = user_installation_roles.user_id
        JOIN gridworks.g_nodes ta
            ON (ta.id = user_installation_roles.ta_g_node_id OR user_installation_roles.ta_g_node_id IS NULL)
            AND ta.g_node_class = 'TerminalAsset'
        LEFT OUTER JOIN gridworks.layouts ON layouts.ta_g_node_id = ta.id
        LEFT OUTER JOIN gridworks.house_operating_status ON house_operating_status.ta_g_node_id = ta.id
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
        LEFT OUTER JOIN (
            SELECT from_alias, payload
            FROM (
                SELECT
                    from_alias,
                    payload,
                    row_number() OVER (PARTITION BY from_alias ORDER BY timestamp DESC) AS row_number
                FROM gridworks.messages
                WHERE message_type_name = 'gw.scada.commit'
            ) AS commits
            WHERE row_number = 1
        ) AS scada_commit ON scada_commit.from_alias = left(ta.alias, -3) || '.scada'
        WHERE gridworks.users.username = :username
    """)

    db_results = await db.execute(sql, {
        "username": username,
    })

    rows = db_results.all()
    # One GitHub question per distinct commit across the fleet, before the
    # summaries are assembled.
    commit_standing = await standings.standings({row[9] for row in rows if row[9] is not None})

    codec = SemaCodec()
    send_time_ms = int(time.time() * 1000)
    result: list[dict] = []
    for row in rows:
        [
            ta_g_node_id,
            g_node_alias,
            role,
            display_name,
            layout_dict,
            operating_status_dict,
            snapshot_dict,
            longest_running_zone_name,
            longest_running_zone_time,
            scada_commit,
        ] = row

        layout: LayoutLite011 | LayoutLite012 | LayoutLite | None = None
        if layout_dict is not None:
            # The row is kept as the version the scada sent; the summary names
            # the versions it carries, so no upgrade here.
            decoded = codec.from_dict(layout_dict, auto_upgrade=False)
            if isinstance(decoded, (LayoutLite011, LayoutLite012, LayoutLite)):
                layout = decoded
            else:
                # The summary word names the layout versions it can carry; a
                # newer layouts row waits for the summary's next version.
                logger.warning(
                    "%s: layouts row is %s %s, outside gw.house.summary; Layout left null",
                    g_node_alias, layout_dict.get("TypeName"), layout_dict.get("Version"),
                )

        operating_status: GwHouseOperatingStatus | None = None
        if operating_status_dict is not None:
            operating_status = codec.from_dict(operating_status_dict, expect=GwHouseOperatingStatus)

        snapshot: SnapshotSpaceheat | None = None
        if snapshot_dict is not None:
            snapshot = codec.from_dict(snapshot_dict, expect=SnapshotSpaceheat)

        summary = GwHouseSummary(
            role=dashboard_role(role),
            ta_g_node_id=str(ta_g_node_id),
            g_node_alias=g_node_alias,
            display_name=display_name,
            layout=layout,
            operating_status=operating_status,
            latest_snapshot_time=datetime_to_sema(datetime.fromtimestamp(snapshot.snapshot_time_unix_ms / 1000, tz=UTC)) if snapshot is not None else None,
            longest_running_zone_name=longest_running_zone_name,
            longest_running_zone_start_time=datetime_to_sema(cast(datetime, longest_running_zone_time)) if longest_running_zone_time is not None else None,
            scada_commit=scada_commit,
            scada_commit_standing=commit_standing[scada_commit] if scada_commit is not None else None,
            send_time_ms=send_time_ms,
        )
        result.append(summary.to_dict())

    return result


def dashboard_role(role: str) -> GwDashboardRole:
    """The roles table's lowercase value as the word's enum; a value the
    enum does not name grants the least."""
    try:
        return GwDashboardRole(role.capitalize())
    except ValueError:
        return GwDashboardRole.default()
