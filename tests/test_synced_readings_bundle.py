"""The readings bundle across a layout change: the rows of one lineage (a
shared name or a shared channel_id) are one series in one scale under the
newest row's name, however the window sits against the change, and a state
row is no series at all (the bundle word labels a series with a unit)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from gw_data.db.models import ReadingChannelSql, ReadingSql
from sqlalchemy import create_engine, insert

from api.sema.enums.gw1_main_auto_state import Gw1MainAutoState
from api.v2.readings_units import IDENTITY, Affine, Scale, affine_between, celsius_scale, fahrenheit_scale

from .conftest import BEECH, OAK, login
from .test_readings_csv import channel

# A day apart from the csv tests' readings, so each module's lineages are its own.
T0 = datetime(2026, 1, 12, 12, 0, tzinfo=UTC)
AUTO = Gw1MainAutoState.enum_name()
AUTO_STATES = Gw1MainAutoState.values()


@pytest.fixture(scope="module")
def readings(pg_url):
    """beech's hp-ewt flips from WaterTempCTimes1000 to CelsiusTimes100 at
    T0+60s and its hp-boss-state from the unversioned "Enum" stamp to the enum
    version; oak's charge-relay1 is renamed charge-relay at the flip under one
    channel_id, and oak's elt-pwr has rows whose units do not reconcile."""
    flip = T0 + timedelta(seconds=60)
    vdc_id = uuid.uuid4()
    lwt_old = channel(BEECH, "hp-ewt", "spaceheat.telemetry.name", "WaterTempCTimes1000", flip)
    lwt_new = channel(BEECH, "hp-ewt", "spaceheat.telemetry.name", "CelsiusTimes100", None)
    depth_old = channel(BEECH, "buffer-depth2", "spaceheat.telemetry.name", "WaterTempCTimes1000", flip)
    depth_new = channel(BEECH, "buffer-depth2", "gw1.unit", "FahrenheitX100", None)
    state_old = channel(BEECH, "hp-boss-state", AUTO, "Enum", flip)
    state_new = channel(BEECH, "hp-boss-state", AUTO, Gw1MainAutoState.enum_version(), None)
    vdc_old = channel(OAK, "charge-relay1", "spaceheat.telemetry.name", "RelayState", flip, vdc_id)
    vdc_new = channel(OAK, "charge-relay", "spaceheat.telemetry.name", "RelayState", None, vdc_id)
    opto_a = channel(OAK, "elt-pwr", "spaceheat.telemetry.name", "BinaryState", flip)
    opto_b = channel(OAK, "elt-pwr", "spaceheat.telemetry.name", "PowerW", None)
    rows = [lwt_old, lwt_new, depth_old, depth_new, state_old, state_new, vdc_old, vdc_new, opto_a, opto_b]
    message_id = uuid.uuid4()

    def reading(ch: dict, at: datetime, value: int) -> dict:
        return {"channel_id": ch["id"], "message_id": message_id, "timestamp": at, "value": value}

    values = [
        # A reading before the window on the row retired inside it: the look-back finds it on the old row.
        reading(lwt_old, T0 - timedelta(minutes=10), 18000),
        reading(lwt_old, T0, 19050), reading(lwt_old, T0 + timedelta(seconds=30), 19070),
        reading(lwt_new, flip, 3590),
        # 20 C as stored by the old data channel, then 68 F as the derived channel sends it.
        reading(depth_old, T0, 20000), reading(depth_new, flip, 6800),
        # The machine changes state twice within one second at the flip.
        reading(state_old, T0, 0), reading(state_new, flip, 1),
        reading(state_new, flip + timedelta(milliseconds=400), 2),
        reading(vdc_old, T0, 1), reading(vdc_new, flip, 0),
        reading(opto_a, T0, 1), reading(opto_b, flip, 0),
    ]
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(insert(ReadingChannelSql), rows)
        conn.execute(insert(ReadingSql), values)
    engine.dispose()
    return rows


async def bundle(client, house: str, channels: str, start: datetime = T0, minutes: int = 2) -> tuple[int, dict]:
    r = await client.get(
        f"/api/v2/installations/{house}/synced.readings.bundle",
        params={"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
                "time_step": 60, "channels": channels},
        headers=await login(client, "sysadmin"),
    )
    return r.status_code, r.json()


def series(body: dict) -> dict[str, dict]:
    return {item["ChannelName"]: item for item in body["ChannelReadingsList"]}


async def test_one_series_in_one_scale_across_the_flip(client, readings):
    code, body = await bundle(client, BEECH, "hp-ewt,buffer-depth2")
    assert code == 200, body
    by_name = series(body)
    assert list(by_name) == ["hp-ewt", "buffer-depth2"]
    assert len(body["TimestampList"]) == 3
    lwt = by_name["hp-ewt"]
    assert (lwt["UnitType"], lwt["Unit"]) == ("spaceheat.telemetry.name", "CelsiusTimes100")
    # The first minute averages the two WaterTempCTimes1000 readings in CelsiusTimes100; the second is the newest
    # row as stored; the third has no readings and carries the last value forward.
    assert lwt["ValueList"] == [1906, 3590, 3590]
    depth = by_name["buffer-depth2"]
    assert (depth["UnitType"], depth["Unit"]) == ("gw1.unit", "FahrenheitX100")
    assert depth["ValueList"] == [6800, 6800, 6800]


async def test_a_window_inside_the_new_era_still_reads_one_series(client, readings):
    # The look-back of thirty minutes reaches the old row's readings; the series is still one.
    code, body = await bundle(client, BEECH, "hp-ewt", start=T0 + timedelta(minutes=1), minutes=1)
    assert code == 200, body
    assert [s["Unit"] for s in body["ChannelReadingsList"]] == ["CelsiusTimes100"]
    assert series(body)["hp-ewt"]["ValueList"] == [3590, 3590]


async def test_a_state_row_is_no_series(client, readings):
    code, body = await bundle(client, BEECH, "hp-boss-state,hp-ewt")
    assert code == 200, body
    assert list(series(body)) == ["hp-ewt"]


async def test_state_sequence_reads_both_the_enum_stamp_and_the_versioned_row(client, readings):
    # The "Enum"-stamped row and the versioned row of one machine are one sequence of symbols, with times to
    # the millisecond so the two changes inside one second at the flip are both there.
    code, body = await bundle(client, BEECH, "hp-ewt")
    assert code == 200, body
    sequences = {s["ChannelName"]: s for s in body["OperatingStateSequenceList"]}
    assert list(sequences) == ["hp-boss-state"]
    assert sequences["hp-boss-state"]["ValueList"] == [AUTO_STATES[0], AUTO_STATES[1], AUTO_STATES[2]]
    assert sequences["hp-boss-state"]["TimestampList"][1:] == ["2026-01-12T12:01:00.000Z", "2026-01-12T12:01:00.400Z"]


async def test_a_rename_is_one_series_under_the_newest_name(client, readings):
    code, body = await bundle(client, OAK, "charge-relay1")
    assert code == 200, body
    assert list(series(body)) == ["charge-relay"]
    assert series(body)["charge-relay"]["ValueList"] == [1, 0, 0]


async def test_rows_whose_units_do_not_reconcile_are_refused(client, readings):
    code, body = await bundle(client, OAK, "elt-pwr")
    assert code == 422, body
    assert "elt-pwr" in body["detail"] and "BinaryState" in body["detail"] and "PowerW" in body["detail"]


def test_affine_between_scales():
    assert affine_between(celsius_scale(1000), celsius_scale(1000)) == IDENTITY
    c_to_f = affine_between(celsius_scale(1000), fahrenheit_scale(100))
    assert c_to_f.apply(100000) == pytest.approx(21200)
    assert affine_between(fahrenheit_scale(100), celsius_scale(1000)).apply(21200) == pytest.approx(100000)
    assert isinstance(affine_between(Scale("flow", 100, 0), Scale("flow", 100, 0)), Affine)
