import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import BigInteger, cast, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bruce.flo_params import FloParamsError, flo_plot_data

from ..dependencies import get_db, require_sys_admin_username

from gw_data.db.models import (
    MessageSql,
)

router = APIRouter()
logger = logging.getLogger(__name__)

FLO_PLOT_DIR = Path("flo-plots")
FLO_PLOT_MAX_AGE_S = 24 * 60 * 60
FLO_PLOT_VERSION = 1
NO_FLO_IN_HOUR = "No FLO was run in this hour"
_NEW_YORK = ZoneInfo("America/New_York")


class FloDataQueryParams(BaseModel):
    time: datetime


def sweep_flo_plot_cache() -> None:
    if not FLO_PLOT_DIR.is_dir():
        return
    cutoff = time.time() - FLO_PLOT_MAX_AGE_S
    for path in FLO_PLOT_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.exception("Could not delete old FLO plot %s", path)


@router.get("/api/v2/installations/{installation_id}/flo.download")
async def get_flo_data(
    installation_id: str,
    query: Annotated[FloDataQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_sys_admin_username)
):
    when = query.time if query.time.tzinfo else query.time.replace(tzinfo=timezone.utc)
    hour_start = when.astimezone(_NEW_YORK).replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    start_unix_s = cast(MessageSql.payload["StartUnixS"].astext, BigInteger)
    from_aliases = (
        f"{installation_id}.ltn",
        f"{installation_id}.scada",
    )
    # LTN publishes bruce FLO params; some journals also attribute the copy that reaches SCADA.
    # Payload GNodeAlias is the house terminal-asset g-node without a role suffix.
    # StartUnixS is the run time shown on the plot. The journal timestamp is only a prune window.
    db_query = (
        select(MessageSql.id, MessageSql.payload)
        .where(
            MessageSql.message_type_name == "flo.params",
            MessageSql.timestamp >= hour_start - timedelta(days=1),
            MessageSql.timestamp < hour_end + timedelta(days=1),
            start_unix_s >= int(hour_start.timestamp()),
            start_unix_s < int(hour_end.timestamp()),
            or_(
                MessageSql.from_alias.in_(from_aliases),
                MessageSql.payload["GNodeAlias"].astext == installation_id,
            ),
        )
        .order_by(desc(start_unix_s), desc(MessageSql.timestamp))
        .limit(1)
    )

    row = (await db.execute(db_query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=NO_FLO_IN_HOUR)
    message_id = str(row.id)
    payload: Any = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail=NO_FLO_IN_HOUR)

    cache_path = FLO_PLOT_DIR / f"{message_id}.json"
    if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < FLO_PLOT_MAX_AGE_S:
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("version") == FLO_PLOT_VERSION:
            print(f"FLO plot cache hit for {installation_id}")
            return cached

    print(f"Running FLO plot for {installation_id}...")
    try:
        plot = await asyncio.to_thread(flo_plot_data, payload)
    except FloParamsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("FLO plot failed for %s", installation_id)
        raise HTTPException(status_code=500, detail=f"FLO plot failed: {e}") from e
    print("Done.")

    plot["version"] = FLO_PLOT_VERSION
    FLO_PLOT_DIR.mkdir(parents=True, exist_ok=True)
    sweep_flo_plot_cache()
    tmp = cache_path.with_name(cache_path.name + ".tmp")
    tmp.write_text(json.dumps(plot))
    tmp.replace(cache_path)
    return plot
