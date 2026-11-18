"""The visualizer's price panel: the price forecast for one house at one
hour, served as the `gw.price.forecast` word the price service published."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot, UUID4Str
from api.sema.types import FloParams, PriceForecast
from api.sema.types.old_versions.flo_params_002 import FloParams002

from ..dependencies import get_current_username, get_db
from ..shared_queries import verify_data_access
from .flo_recommendation import slot_start_s

from gw_data.db.models import MessageSql

router = APIRouter()

NO_FORECAST = "No price forecast for this hour"

# Versions that name a PriceForecastId. 001 does not; the journal's 002 does,
# and a re-run's 003 does too.
FORECAST_NAMING_PARAMS = (FloParams, FloParams002)


async def forecast_the_request_named(
    db: AsyncSession, installation_id: str, slot_s: int,
) -> UUID4Str | None:
    """The `PriceForecastId` of the newest `flo.params` the house's LTN sent
    for the slot starting at `slot_s`; None when the slot has no request, or
    only requests at a version that names no forecast."""
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == FloParams.type_name_value(),
            MessageSql.payload["Version"].astext.in_(
                [cls.version_value() for cls in FORECAST_NAMING_PARAMS]
            ),
            MessageSql.payload["GNodeAlias"].astext == installation_id,
            MessageSql.payload["StartUnixS"].astext == str(slot_s),
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(1)
    )
    row = (await db.execute(db_query)).first()
    if row is None:
        return None
    word = SemaCodec().from_dict(row[0], auto_upgrade=False)
    if not isinstance(word, FORECAST_NAMING_PARAMS):
        return None
    return word.price_forecast_id


async def forecast_by_id(db: AsyncSession, forecast_uid: UUID4Str) -> PriceForecast | None:
    """The journaled `gw.price.forecast` with this `ForecastUid`."""
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == PriceForecast.type_name_value(),
            MessageSql.payload["ForecastUid"].astext == forecast_uid,
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(1)
    )
    row = (await db.execute(db_query)).first()
    return None if row is None else SemaCodec().from_dict(row[0], expect=PriceForecast)


async def forecast_in_force(db: AsyncSession, time: datetime) -> PriceForecast | None:
    """The journaled `gw.price.forecast` with the greatest `CreatedMs` at or
    before `time`: the forecast any bidder had at that moment."""
    created_ms = MessageSql.payload["CreatedMs"].astext.cast(BigInteger)
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == PriceForecast.type_name_value(),
            created_ms <= int(time.timestamp() * 1000),
        )
        .order_by(created_ms.desc())
        .limit(1)
    )
    row = (await db.execute(db_query)).first()
    return None if row is None else SemaCodec().from_dict(row[0], expect=PriceForecast)


@router.get("/api/v2/installations/{installation_id}/price-forecast", response_model=None)
async def get_price_forecast(
    installation_id: LeftRightDot,
    time: datetime = Query(),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """The price forecast this house had for the market slot `time` falls
    in, in the word's dict form. First the forecast the house's own
    `flo.params` for that slot names, so the panel shows the prices the
    FLO was asked with; otherwise the newest forecast the price service had
    published by `time`. A 404 when the journal has neither.

    Which of the two chose the forecast is not stated in the response: a
    sema enum for it is pending, derived here and sent as data.
    """
    await verify_data_access(db, username, installation_id, effective_date=time)
    named = await forecast_the_request_named(db, installation_id, slot_start_s(time))
    forecast = None if named is None else await forecast_by_id(db, named)
    if forecast is None:
        forecast = await forecast_in_force(db, time)
    if forecast is None:
        raise HTTPException(status_code=404, detail=NO_FORECAST)
    return forecast.to_dict()
