"""The price-forecast route forwards the stopgap service's word unchanged and
turns a service that cannot answer into a 503."""

import httpx
import pytest

from api.v2.routers import price_forecast

from .conftest import login, sample


class StubClient:
    """Stands in for httpx.AsyncClient: answers every GET with one response."""

    response: httpx.Response | None = None
    error: Exception | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str) -> httpx.Response:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.fixture
def stub(monkeypatch) -> type[StubClient]:
    StubClient.response = None
    StubClient.error = None
    monkeypatch.setattr(price_forecast.httpx, "AsyncClient", StubClient)
    return StubClient


async def test_forwards_the_word(client, stub):
    word = sample("gw.stopgap.price.forecast.000.json")
    stub.response = httpx.Response(200, json=word)
    r = await client.get("/api/v2/price-forecast/latest", headers=await login(client, "beech-viewer"))
    assert r.status_code == 200, r.text
    assert r.json() == word


async def test_service_without_a_forecast_is_503(client, stub):
    stub.response = httpx.Response(503, json={"detail": "no price forecast available"})
    r = await client.get("/api/v2/price-forecast/latest", headers=await login(client, "beech-viewer"))
    assert r.status_code == 503


async def test_unreachable_service_is_503(client, stub):
    stub.error = httpx.ConnectError("refused")
    r = await client.get("/api/v2/price-forecast/latest", headers=await login(client, "beech-viewer"))
    assert r.status_code == 503


async def test_requires_a_login(client, stub):
    r = await client.get("/api/v2/price-forecast/latest")
    assert r.status_code == 401
