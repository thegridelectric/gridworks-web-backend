import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, cast, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bruce.flo_params import FloParamsError, flo_bid_plot_data, flo_plot_data

from ..dependencies import get_db, require_sys_admin_username

from gw_data.db.models import (
    MessageSql,
)

router = APIRouter()
logger = logging.getLogger(__name__)

FLO_PLOT_DIR = Path("flo-plots")
FLO_PLOT_MAX_AGE_S = 24 * 60 * 60
FLO_PLOT_VERSION = 3
NO_FLO_IN_HOUR = "No FLO was run in this hour"
_NEW_YORK = ZoneInfo("America/New_York")


class FloDataQueryParams(BaseModel):
    time: datetime


class FloPrefetchQueryParams(BaseModel):
    time: datetime
    hours: int = Field(ge=1, le=48)


def _ny_hour(when: datetime) -> datetime:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_NEW_YORK).replace(minute=0, second=0, microsecond=0)


def _read_cached_plot(message_id: str) -> dict | None:
    path = FLO_PLOT_DIR / f"{message_id}.json"
    if not path.is_file() or time.time() - path.stat().st_mtime >= FLO_PLOT_MAX_AGE_S:
        return None
    try:
        cached = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(cached, dict) and cached.get("version") == FLO_PLOT_VERSION:
        return cached
    return None


def _write_cached_plot(message_id: str, plot: dict) -> None:
    FLO_PLOT_DIR.mkdir(parents=True, exist_ok=True)
    sweep_flo_plot_cache()
    path = FLO_PLOT_DIR / f"{message_id}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plot))
    tmp.replace(path)


async def _flo_row_for_hour(db: AsyncSession, installation_id: str, hour_start: datetime):
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
    return (await db.execute(db_query)).one_or_none()


async def _real_price_mwh_near_hour_start(db: AsyncSession, hour_start: datetime) -> float | None:
    """PriceTimes1000 / 1000 of the first latest.price persisted near the top of the NY hour (any from_alias)."""
    slot_unix = int(hour_start.timestamp())
    db_query = (
        select(MessageSql.payload)
        .where(
            MessageSql.message_type_name == "latest.price",
            MessageSql.timestamp >= hour_start - timedelta(days=1),
            or_(
                MessageSql.payload["MarketSlotName"].astext.like(f"%.{slot_unix}"),
                MessageSql.timestamp >= hour_start - timedelta(minutes=5),
            ),
            MessageSql.timestamp < hour_start + timedelta(minutes=30),
        )
        .order_by(MessageSql.timestamp)
        .limit(1)
    )
    row = (await db.execute(db_query)).one_or_none()
    if row is None:
        return None
    payload: Any = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("PriceTimes1000") is None:
        return None
    return float(payload["PriceTimes1000"]) / 1000


async def _plot_for_hour(db: AsyncSession, installation_id: str, hour_start: datetime) -> dict | None:
    row = await _flo_row_for_hour(db, installation_id, hour_start)
    if row is None:
        return None
    payload: Any = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    message_id = str(row.id)
    cached = _read_cached_plot(message_id)
    if cached is not None:
        print(f"FLO plot cache hit for {installation_id}")
        return cached
    print(f"Running FLO plot for {installation_id}...")
    plot = await asyncio.to_thread(flo_plot_data, payload)
    print("Done.")
    plot["version"] = FLO_PLOT_VERSION
    _write_cached_plot(message_id, plot)
    return plot


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
    hour_start = _ny_hour(query.time)
    try:
        plot = await _plot_for_hour(db, installation_id, hour_start)
        if plot is None:
            raise HTTPException(status_code=404, detail=NO_FLO_IN_HOUR)
        # Not cached with the plot: the price for the slot can arrive after the FLO ran.
        real_price_mwh = await _real_price_mwh_near_hour_start(db, hour_start)
        bid = flo_bid_plot_data(plot["bid"], real_price_mwh=real_price_mwh)
    except HTTPException:
        raise
    except FloParamsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("FLO plot failed for %s", installation_id)
        raise HTTPException(status_code=500, detail=f"FLO plot failed: {e}") from e
    return {"summary": plot["summary"], "bid": bid}


@router.post("/api/v2/installations/{installation_id}/flo.prefetch")
async def prefetch_flo_data(
    installation_id: str,
    query: Annotated[FloPrefetchQueryParams, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(require_sys_admin_username)
):
    hour_start = _ny_hour(query.time)
    ready = 0
    for hours_back in range(query.hours - 1, -1, -1):
        try:
            plot = await _plot_for_hour(db, installation_id, hour_start - timedelta(hours=hours_back))
        except Exception:
            logger.exception("FLO prefetch failed for %s", installation_id)
            continue
        if plot is not None:
            ready += 1
    return {"ready": ready}
