"""The latest-prices route serves the maker's cleared prices for the slots a
window touches, oldest first, every row as latest.price 001."""

from datetime import UTC, datetime, timedelta

from .conftest import CLEARED_SLOTS, CLEARED_VERSIONS, FLO_SLOT_S, MARKET, cleared_price_served, login


def window(start_s: int, end_s: int) -> dict:
    return {
        "start": datetime.fromtimestamp(start_s, tz=UTC).isoformat(),
        "end": datetime.fromtimestamp(end_s, tz=UTC).isoformat(),
    }


async def test_a_window_inside_one_slot_gets_that_slot_and_the_one_before(client):
    r = await client.get(
        f"/api/v2/markets/{MARKET}/latest-prices",
        params=window(FLO_SLOT_S + 600, FLO_SLOT_S + 2400),
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == [cleared_price_served(FLO_SLOT_S, "000")]


async def test_a_window_over_the_slots_gets_them_oldest_first_at_001(client):
    r = await client.get(
        f"/api/v2/markets/{MARKET}/latest-prices",
        params=window(FLO_SLOT_S - 1800, FLO_SLOT_S + 7200),
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == [
        cleared_price_served(slot, version) for slot, version in zip(CLEARED_SLOTS, CLEARED_VERSIONS, strict=True)
    ]
    assert {p["Version"] for p in r.json()} == {"001"}


async def test_a_001_row_keeps_its_parts(client):
    r = await client.get(
        f"/api/v2/markets/{MARKET}/latest-prices",
        params=window(FLO_SLOT_S + 3600, FLO_SLOT_S + 3600 + 60),
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    (price,) = r.json()
    assert [c["Name"] for c in price["Components"]] == ["WholesaleEnergy", "WholesaleCongestion", "WholesaleLoss", "Delivery"]
    assert sum(c["PriceX1000"] for c in price["Components"]) == price["PriceX1000"]


async def test_a_market_that_cleared_nothing_is_empty(client):
    r = await client.get(
        "/api/v2/markets/e.rt60gate5.hw1.isone.nowhere/latest-prices",
        params=window(FLO_SLOT_S - 1800, FLO_SLOT_S + 7200),
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_needs_a_login(client):
    r = await client.get(f"/api/v2/markets/{MARKET}/latest-prices", params=window(FLO_SLOT_S, FLO_SLOT_S + 60))
    assert r.status_code == 401
