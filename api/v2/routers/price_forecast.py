"""The visualizer's price panel: the stopgap price service's latest forecast,
forwarded as the `gw.stopgap.price.forecast` word it serves."""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from api.config import Settings
from api.sema.codec import SemaCodec
from api.sema.types import GwStopgapPriceForecast

from ..dependencies import get_current_username, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/v2/price-forecast/latest", response_model=None)
async def get_latest_price_forecast(
    settings: Settings = Depends(get_settings),
    username: str = Depends(get_current_username),
) -> dict:
    """The stopgap price service's current forecast, one pricing node under
    one tariff for the whole fleet, in the word's dict form. A 503 when the
    service has no forecast to serve or cannot be reached."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(settings.price_forecast_url)
    except httpx.HTTPError as e:
        logger.warning("price service unreachable: %s", e)
        raise HTTPException(status_code=503, detail="price service unreachable") from e
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail=f"price service answered {response.status_code}")
    forecast = SemaCodec().from_dict(response.json(), expect=GwStopgapPriceForecast)
    return forecast.to_dict()
