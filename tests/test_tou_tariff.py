"""The tou-tariff route answers the fleet tariff in `gw.tou.tariff` wire form."""

import re

from .conftest import login

HH_MM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


async def test_current_tariff_is_the_word(client):
    r = await client.get("/api/v2/tou-tariff/current", headers=await login(client, "beech-viewer"))
    assert r.status_code == 200, r.text
    word = r.json()
    assert word["TypeName"] == "gw.tou.tariff"
    assert word["Version"] == "000"
    assert word["TimezoneStr"] == "America/New_York"
    assert len(word["OnPeakWindows"]) == 2
    for window in word["OnPeakWindows"]:
        assert window["TypeName"] == "gw.tou.window"
        assert HH_MM.match(window["Start"]) and HH_MM.match(window["End"])
        assert window["Start"] < window["End"]
        assert window["Days"] and set(window["Days"]) <= DAYS
        assert len(set(window["Days"])) == len(window["Days"])


async def test_tariff_needs_a_login(client):
    r = await client.get("/api/v2/tou-tariff/current")
    assert r.status_code == 401
