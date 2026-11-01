"""The weather routes serve a bundle's record and its journaled emissions as
the words the weather service published, each at its own version, filtered
by bundle and window."""

from datetime import timedelta

from .conftest import BUNDLE, FORECAST_CREATED, login, sample, forecast_emission


async def test_bundle_record_is_the_word(client):
    r = await client.get(f"/api/v2/weather/bundles/{BUNDLE}", headers=await login(client, "beech-viewer"))
    assert r.status_code == 200, r.text
    assert r.json() == sample("gw.weather.forecast.bundle.gt.002.json")


async def test_unknown_bundle_is_404(client):
    r = await client.get("/api/v2/weather/bundles/us.me.nowhere", headers=await login(client, "beech-viewer"))
    assert r.status_code == 404


async def test_forecasts_in_window_oldest_first(client):
    start, end = FORECAST_CREATED[1] - timedelta(minutes=1), FORECAST_CREATED[2] + timedelta(minutes=1)
    r = await client.get(
        f"/api/v2/weather/bundles/{BUNDLE}/forecasts",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == [
        forecast_emission(FORECAST_CREATED[1], "001"),
        forecast_emission(FORECAST_CREATED[2], "002"),
    ]


async def test_a_bundle_that_never_emitted_has_no_forecasts(client):
    start, end = FORECAST_CREATED[0] - timedelta(days=1), FORECAST_CREATED[2] + timedelta(days=1)
    r = await client.get(
        "/api/v2/weather/bundles/us.me.millinocket.bobcat/forecasts",
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_window_must_be_ordered(client):
    at = FORECAST_CREATED[0]
    r = await client.get(
        f"/api/v2/weather/bundles/{BUNDLE}/forecasts",
        params={"start": at.isoformat(), "end": at.isoformat()},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 422


async def test_needs_a_login(client):
    r = await client.get(f"/api/v2/weather/bundles/{BUNDLE}")
    assert r.status_code == 401
