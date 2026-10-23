"""The readings csv across a layout change: the rows of one lineage (a shared
name or a shared channel_id) are one column in one scale under the newest
row's name, and a state column reads as symbols whatever the enum version of
the row behind each reading."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from gw_data.db.models import ReadingChannelSql, ReadingSql
from sqlalchemy import create_engine, insert

from api.sema.enums.gw1_main_auto_state import Gw1MainAutoState
from api.v2.readings_units import ChannelUnit, UnitConflict, column_values, lineages

from .conftest import BEECH, OAK, login

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
AUTO = Gw1MainAutoState.enum_name()
AUTO_VERSION = Gw1MainAutoState.enum_version()
AUTO_STATES = Gw1MainAutoState.values()


def channel(
    alias: str, name: str, unit_type: str, unit: str, retired_at: datetime | None, channel_id: uuid.UUID | None = None
) -> dict:
    """A reading_channels row; `channel_id` is the layout's Id, None where no layout names the pair."""
    return {
        "id": uuid.uuid4(), "name": name, "terminal_asset_alias": alias + ".ta", "display_name": name,
        "unit": unit, "unit_type": unit_type, "channel_type": "data", "deactivated_date": retired_at,
        "channel_id": channel_id,
    }


@pytest.fixture(scope="module")
def readings(pg_url):
    """beech's hp-lwt flips from WaterTempCTimes1000 to CelsiusTimes100 at
    T0+60s, with a reading on an Unknown-telemetry row in between that the
    csv leaves out, and its top-state row from the unversioned "Enum" stamp
    to the enum version; oak's opto has rows whose units do not reconcile, and
    oak's vdc-relay1 is renamed vdc-relay at the flip under one channel_id."""
    flip = T0 + timedelta(seconds=60)
    vdc_id = uuid.uuid4()
    vdc_old = channel(OAK, "vdc-relay1", "spaceheat.telemetry.name", "RelayState", flip, vdc_id)
    vdc_new = channel(OAK, "vdc-relay", "spaceheat.telemetry.name", "RelayState", None, vdc_id)
    lwt_old = channel(BEECH, "hp-lwt", "spaceheat.telemetry.name", "WaterTempCTimes1000", flip)
    lwt_new = channel(BEECH, "hp-lwt", "spaceheat.telemetry.name", "CelsiusTimes100", None)
    lwt_none = channel(BEECH, "hp-lwt", "spaceheat.telemetry.name", "Unknown", T0)
    state_old = channel(BEECH, "top-state", AUTO, "Enum", flip)
    state_new = channel(BEECH, "top-state", AUTO, AUTO_VERSION, None)
    depth_old = channel(BEECH, "buffer-depth1", "spaceheat.telemetry.name", "WaterTempCTimes1000", flip)
    depth_new = channel(BEECH, "buffer-depth1", "gw1.unit", "FahrenheitX100", None)
    opto_a = channel(OAK, "opto", "spaceheat.telemetry.name", "BinaryState", flip)
    opto_b = channel(OAK, "opto", "spaceheat.telemetry.name", "PowerW", None)
    rows = [lwt_old, lwt_new, lwt_none, state_old, state_new, depth_old, depth_new, opto_a, opto_b, vdc_old, vdc_new]
    message_id = uuid.uuid4()

    def reading(ch: dict, at: datetime, value: int) -> dict:
        return {"channel_id": ch["id"], "message_id": message_id, "timestamp": at, "value": value}

    values = [
        # A reading before the window on a row that is retired inside it: the carry-forward look-back sees it.
        reading(lwt_old, T0 - timedelta(minutes=10), 18000),
        reading(lwt_old, T0, 19050), reading(lwt_old, T0 + timedelta(seconds=30), 19070),
        reading(lwt_new, flip, 3590),
        reading(lwt_none, T0 + timedelta(seconds=45), 1),
        reading(state_old, T0, 0), reading(state_new, flip, 1),
        # 20 C as stored by the old data channel, then 68 F as the derived channel sends it.
        reading(depth_old, T0, 20000), reading(depth_new, flip, 6800),
        reading(opto_a, T0, 1), reading(opto_b, flip, 0),
        reading(vdc_old, T0, 1), reading(vdc_new, flip, 0),
    ]
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(insert(ReadingChannelSql), rows)
        conn.execute(insert(ReadingSql), values)
    engine.dispose()
    return rows


async def download(client, house: str, channels: str, **params) -> tuple[int, str]:
    r = await client.get(
        f"/api/v2/installations/{house}/readings.download",
        params={"start": T0.isoformat(), "end": (T0 + timedelta(seconds=120)).isoformat(), "time_step": 60,
                "channels": channels, **params},
        headers=await login(client, "sysadmin"),
    )
    return r.status_code, r.text


async def test_one_column_in_one_scale_across_the_flip(client, readings):
    code, body = await download(client, BEECH, "hp-lwt,buffer-depth1,top-state")
    assert code == 200, body
    table = list(csv.reader(io.StringIO(body)))
    assert table[1] == ["timestamp", "hp-lwt", "buffer-depth1", "top-state"]
    cells = {row[0][-8:]: row[1:] for row in table[2:]}
    # The first minute: hp-lwt averages two WaterTempCTimes1000 readings in the newest row's CelsiusTimes100;
    # buffer-depth1's 20 C is in the newest row's FahrenheitX100.
    assert cells["07:00:00"] == ["1906", "6800", AUTO_STATES[0]]
    # The second minute is each newest row as stored, and the state's symbol in the versioned row.
    assert cells["07:01:00"] == ["3590", "6800", AUTO_STATES[1]]
    # The third minute has no readings; every cell carries forward.
    assert cells["07:02:00"] == ["3590", "6800", AUTO_STATES[1]]


async def test_rows_whose_units_do_not_reconcile_are_refused(client, readings):
    code, body = await download(client, OAK, "opto")
    assert code == 422, body
    assert "opto" in body and "BinaryState" in body and "PowerW" in body


async def test_a_rename_is_one_column_under_the_newest_name(client, readings):
    # Asked for by the old name, the column comes whole and heads with the newest name.
    code, body = await download(client, OAK, "vdc-relay1")
    assert code == 200, body
    table = list(csv.reader(io.StringIO(body)))
    assert table[1] == ["timestamp", "vdc-relay"]
    assert [row[1] for row in table[2:]] == ["1", "0", "0"]


def row(name: str, unit_type: str, unit: str, retired_at: datetime | None, channel_id: uuid.UUID | None = None) -> ChannelUnit:
    return ChannelUnit(uuid.uuid4(), name, unit_type, unit, retired_at, channel_id)


def test_lineages_knit_by_name_and_by_channel_id():
    vdc_id = uuid.uuid4()
    rows = [
        # A unit change under one name mints a new Id; a rename keeps the Id; a pseudo row (no Id) joins by name.
        row("dist-swt", "spaceheat.telemetry.name", "WaterTempCTimes1000", T0, uuid.uuid4()),
        row("dist-swt", "spaceheat.telemetry.name", "CelsiusTimes100", None, uuid.uuid4()),
        row("vdc-relay1", "spaceheat.telemetry.name", "RelayState", T0, vdc_id),
        row("vdc-relay", "spaceheat.telemetry.name", "RelayState", None, vdc_id),
        row("heat-call", "gw1.unit", "Unitless", T0),
        row("heat-call", "gw1.unit", "Unitless", None, uuid.uuid4()),
        row("hp-lwt", "spaceheat.telemetry.name", "Unknown", None),
    ]
    columns = {c.name: c for c in lineages(rows)}
    assert set(columns) == {"dist-swt", "vdc-relay", "heat-call"}
    assert columns["vdc-relay"].names == {"vdc-relay1", "vdc-relay"}
    assert len(columns["dist-swt"].rows) == 2 and len(columns["heat-call"].rows) == 2


def test_cell_rules():
    t_old = row("t", "gw1.unit", "FahrenheitX100", T0)
    t_new = row("t", "spaceheat.telemetry.name", "WaterTempCTimes1000", None)
    s_old = row("s", AUTO, "Enum", T0)
    rules = column_values(lineages([t_old, t_new, row("t", "spaceheat.telemetry.name", "Unknown", T0), s_old]))
    assert len(rules) == 3
    # The column's unit is the active row's, and the retired Fahrenheit row converts into it.
    assert {r.column_unit for r in rules.values()} == {"WaterTempCTimes1000", AUTO}
    assert round(rules[t_old.id].convert(21200)) == 100000
    assert rules[s_old.id].convert(len(AUTO_STATES) + 5) == len(AUTO_STATES) + 5
    with pytest.raises(UnitConflict):
        column_values(lineages([row("s", AUTO, "999", None)]))
    with pytest.raises(UnitConflict):
        column_values(lineages([
            row("p", "spaceheat.telemetry.name", "PowerW", None),
            row("p", "spaceheat.telemetry.name", "GpmTimes100", T0),
        ]))
