"""The house price-forecast route serves the forecast the house's own request
for the slot names, else the newest forecast the price service had published
by then, as the `gw.price.forecast` word, and knows an hour with neither."""

from datetime import timedelta

from .conftest import (
    BEECH, EARLIER_FORECAST_CREATED, EARLIER_FORECAST_UID, FLO_SLOT_S, LATER_FORECAST_CREATED, LATER_FORECAST_UID,
    OAK, login, service_forecast,
)
from .test_flo_recommendation import at


async def test_the_forecast_the_houses_request_names(client):
    r = await client.get(
        f"/api/v2/installations/{OAK}/price-forecast",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "sysadmin"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == service_forecast(EARLIER_FORECAST_UID, EARLIER_FORECAST_CREATED)


async def test_a_house_whose_request_names_no_forecast_gets_the_newest_published(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/price-forecast",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == service_forecast(LATER_FORECAST_UID, LATER_FORECAST_CREATED)


async def test_an_hour_without_a_request_gets_the_forecast_in_force_then(client):
    between = EARLIER_FORECAST_CREATED + timedelta(minutes=30)
    r = await client.get(
        f"/api/v2/installations/{BEECH}/price-forecast",
        params={"time": between.isoformat()},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ForecastUid"] == EARLIER_FORECAST_UID


async def test_an_hour_before_any_forecast_is_404(client):
    before = EARLIER_FORECAST_CREATED - timedelta(hours=1)
    r = await client.get(
        f"/api/v2/installations/{BEECH}/price-forecast",
        params={"time": before.isoformat()},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "No price forecast for this hour"


async def test_a_viewer_of_another_house_sees_nothing(client):
    r = await client.get(
        f"/api/v2/installations/{OAK}/price-forecast",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 404


async def test_needs_a_login(client):
    r = await client.get(f"/api/v2/installations/{BEECH}/price-forecast", params={"time": at(FLO_SLOT_S)})
    assert r.status_code == 401
