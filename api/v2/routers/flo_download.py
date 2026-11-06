import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from bruce.flo_params import FloParamsError, render_flo_params_png

from ..dependencies import get_db, require_sys_admin_username

from gw_data.db.models import (
    MessageSql,
)

router = APIRouter()
logger = logging.getLogger(__name__)

class FloDataQueryParams(BaseModel):
    time: datetime


@router.get("/api/v2/installations/{installation_id}/flo.download")
async def get_flo_data(
    installation_id: str,
    query: Annotated[FloDataQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_sys_admin_username)
):
    time_start = query.time - timedelta(days=2)
    time_end = query.time
    from_aliases = (
        f"{installation_id}.ltn",
        f"{installation_id}.scada",
    )
    # LTN publishes bruce FLO params; some journals also attribute the copy that reaches SCADA.
    # Payload GNodeAlias is the house terminal-asset g-node without a role suffix.
    db_query = (
        select(MessageSql.payload)
        .where(
            MessageSql.message_type_name == "flo.params",
            MessageSql.timestamp >= time_start,
            MessageSql.timestamp <= time_end,
            or_(
                MessageSql.from_alias.in_(from_aliases),
                MessageSql.payload["GNodeAlias"].astext == installation_id,
            ),
        )
        .order_by(desc(MessageSql.timestamp))
        .limit(1)
    )

    db_result = await db.execute(db_query)
    payload: Any = db_result.scalars().one_or_none()
    if isinstance(payload, str):
        payload = json.loads(payload)

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=404,
            detail="No flo.params found in the two days before the end time",
        )

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    print(f"Running FLO plot for {installation_id}...")
    try:
        await asyncio.to_thread(render_flo_params_png, payload, path)
    except FloParamsError as e:
        os.unlink(path)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        os.unlink(path)
        logger.exception("FLO plot failed for %s", installation_id)
        raise HTTPException(status_code=500, detail=f"FLO plot failed: {e}") from e
    print("Done.")

    return FileResponse(
        path,
        media_type="image/png",
        filename=f"{installation_id.rsplit('.', 1)[-1]}-flo.png",
        background=BackgroundTask(os.unlink, path),
    )
