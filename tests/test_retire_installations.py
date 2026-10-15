"""The house list and the data-access check against the g_nodes projection:
roles keyed by the TerminalAsset GNodeId, nothing about a person in any
response."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
import pytest

from api.v2.dependencies import decode_token
from api.v2.shared_queries import verify_data_access

from .conftest import BEECH, BEECH_TA_ID, OAK, OAK_TA_ID, SNAPSHOT_TIME_MS, Seed, login

PERSON_FIELDS = {"Address", "PrimaryContact", "SecondaryContact", "HardwareLayout", "SpaceheatParameters"}


async def test_sysadmin_sees_every_house_keyed_by_ta(client):
    r = await client.get("/api/v2/installations/*/summaries", headers=await login(client, "sysadmin"))
    assert r.status_code == 200, r.text
    houses = {h["GNodeAlias"]: h for h in r.json()}
    assert set(houses) == set(Seed.houses)
    beech, oak = houses[BEECH], houses[OAK]
    assert beech["TaGNodeId"] == str(BEECH_TA_ID)
    assert oak["TaGNodeId"] == str(OAK_TA_ID)
    assert beech["Role"] == "admin"
    assert beech["DisplayName"] == "Beech"
    for h in (beech, oak):
        assert not PERSON_FIELDS & set(h)
        assert h["LayoutLite"]["TypeName"] == "layout.lite"
        assert h["SystemMode"] == "Heating"
    assert beech["LatestSnapshotTime"] == datetime.fromtimestamp(SNAPSHOT_TIME_MS / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert oak["LatestSnapshotTime"] is None


async def test_owner_sees_only_their_house(client):
    r = await client.get("/api/v2/installations/*/summaries", headers=await login(client, "beech-owner"))
    assert r.status_code == 200, r.text
    assert [(h["GNodeAlias"], h["Role"]) for h in r.json()] == [(BEECH, "owner")]


async def test_session_sys_admin_is_an_admin_role_on_every_house(client):
    for username, is_admin in (("sysadmin", True), ("beech-owner", False)):
        token = (await login(client, username))["Authorization"].removeprefix("Bearer ")
        assert decode_token(token)["adm"] is is_admin


@pytest.mark.parametrize("username,house,age_days,allowed", [
    ("beech-owner", BEECH, 0, True),
    ("beech-viewer", BEECH, 0, False),
    ("beech-viewer", BEECH, 11, True),
    ("beech-owner", OAK, 11, False),
    ("sysadmin", OAK, 0, True),
])
async def test_verify_data_access(db_session, username, house, age_days, allowed):
    effective = datetime.now(tz=UTC) - timedelta(days=age_days)
    async with db_session() as db:
        if allowed:
            await verify_data_access(db, username, house, effective)
        else:
            with pytest.raises(HTTPException) as e:
                await verify_data_access(db, username, house, effective)
            assert e.value.status_code == 404


async def test_hourly_electricity_joins_roles_through_the_ta(client):
    end = datetime.now(tz=UTC)
    start = end - timedelta(days=5)
    params = {"start": start.isoformat(), "end": end.isoformat()}
    both = f"/api/v2/installations/{BEECH},{OAK}/hourly.electricity"

    r = await client.get(both, params=params, headers=await login(client, "sysadmin"))
    assert r.status_code == 200, r.text
    assert [row[1:] for row in r.json()] == [[4.0, 50.0]]

    r = await client.get(both, params=params, headers=await login(client, "beech-owner"))
    assert [row[1:] for row in r.json()] == [[1.5, 40.0]]

    # A viewer sees nothing newer than ten days.
    r = await client.get(both, params=params, headers=await login(client, "beech-viewer"))
    assert r.json() == []
