"""The FLO route serves the recommendation the FLO service published for a
house's slot as the word, at the version the journal holds, serves the LTN's
request the same way, and knows a
slot no FLO answered."""

from datetime import UTC, datetime, timedelta

from .conftest import BEECH, FLO_PREVIOUS_SLOT_S, FLO_SLOT_S, OAK, flo_params, login, recommendation


def at(slot_start_s: int, minutes: int = 20) -> str:
    return (datetime.fromtimestamp(slot_start_s, tz=UTC) + timedelta(minutes=minutes)).isoformat()


async def test_the_slots_recommendation_is_the_word(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/flo.recommendation",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == recommendation("001", BEECH, FLO_SLOT_S)


async def test_an_older_slot_is_served_at_its_own_version(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/flo.recommendation",
        params={"time": at(FLO_PREVIOUS_SLOT_S, 59)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == recommendation("000", BEECH, FLO_PREVIOUS_SLOT_S)


async def test_a_slot_no_flo_answered_is_404(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/flo.recommendation",
        params={"time": at(FLO_SLOT_S + 3600)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "No FLO was run in this hour"


async def test_another_houses_slot_is_its_own(client):
    r = await client.get(
        f"/api/v2/installations/{OAK}/flo.recommendation",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "sysadmin"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["BidderAlias"] == OAK


async def test_a_viewer_of_another_house_sees_nothing(client):
    r = await client.get(
        f"/api/v2/installations/{OAK}/flo.recommendation",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 404


async def test_the_slots_request_is_the_params_word(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/flo.params",
        params={"time": at(FLO_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert r.json() == flo_params(BEECH, FLO_SLOT_S)


async def test_a_slot_the_ltn_did_not_request_is_404(client):
    r = await client.get(
        f"/api/v2/installations/{BEECH}/flo.params",
        params={"time": at(FLO_PREVIOUS_SLOT_S)},
        headers=await login(client, "beech-viewer"),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "No FLO was run in this hour"
