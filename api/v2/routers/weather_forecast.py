"""The visualizer's weather panel: a weather bundle's record and its journaled
forecast emissions, served as the words the weather service publishes."""

from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot
from api.sema.types import WeatherForecast, WeatherForecastBundleGt
from api.sema.types.old_versions.weather_forecast_001 import WeatherForecast001

from ..dependencies import get_current_username, get_db

from gw_data.db.models import MessageSql

router = APIRouter()

# The journal keeps every emission; a window of a few days at an hourly
# bundle is well under this, and a wider ask gets the newest runs.
MAX_FORECASTS = 500

# The versions the journal holds: 001 rows have no ForecastUid and no
# upgrade path to 002, so each is served at its own version.
JOURNALED_FORECAST = (WeatherForecast, WeatherForecast001)


class ForecastWindow(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end must be after start")
        return self


@router.get("/api/v2/weather/bundles/{bundle_name}", response_model=None)
async def get_bundle(
    bundle_name: LeftRightDot,
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """The newest `gw.weather.forecast.bundle.gt` record the journal holds
    under this name, in the word's dict form: the forecast channels whose
    series the bundle's emissions hold and the observed channels, with
    their units, that each targets. A 404 when the journal has no record."""
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == WeatherForecastBundleGt.type_name_value(),
            MessageSql.payload["Name"].astext == bundle_name,
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(1)
    )
    row = (await db.execute(db_query)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no bundle record named {bundle_name}")
    bundle = SemaCodec().from_dict(row[0], expect=WeatherForecastBundleGt)
    return bundle.to_dict()


@router.get("/api/v2/weather/bundles/{bundle_name}/forecasts", response_model=None)
async def get_forecasts(
    bundle_name: LeftRightDot,
    window: Annotated[ForecastWindow, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> list[dict]:
    """The bundle's `gw.weather.forecast` emissions the journal received in
    the window, oldest first, each in the dict form of its own version.
    Values are the scaled integers of the word; the bundle record gives
    their units."""
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == WeatherForecast.type_name_value(),
            MessageSql.payload["BundleName"].astext == bundle_name,
            MessageSql.timestamp >= window.start,
            MessageSql.timestamp <= window.end,
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(MAX_FORECASTS)
    )
    rows = (await db.execute(db_query)).all()
    codec = SemaCodec()
    forecasts = []
    for row in reversed(rows):
        forecast = codec.from_dict(row[0], auto_upgrade=False)
        if not isinstance(forecast, JOURNALED_FORECAST):
            raise ValueError(f"decoded {type(forecast).__name__}, expected a gw.weather.forecast")
        forecasts.append(forecast)
    return [forecast.to_dict() for forecast in forecasts]
