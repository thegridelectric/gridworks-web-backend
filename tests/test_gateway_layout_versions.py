"""The gateway reads the layout.lite its houses send: a live layout at the
fleet's version sets the house's layout, and a layouts row the snapshot
cannot read costs that row alone."""

import json

from gateway.consumer import handle_message
from gateway.layouts import apply_layout_rows
from gateway.sema import SemaCodec
from gateway.sema.types import LayoutLite
from gateway.state import HouseState, HouseStateStore

from .conftest import ROOT

SAMPLES = ROOT / "gateway" / "sema" / "samples"
OAK_SCADA = "hw1.isone.me.versant.keene.oak.scada"
BEECH_SCADA = "hw1.isone.me.versant.keene.beech.scada"


def layout_payload(scada_alias: str) -> dict:
    """The snapshot's sample of the latest layout.lite, from one scada."""
    sample = json.loads((SAMPLES / f"layout.lite.{LayoutLite.version_value()}.json").read_text())
    return {**sample, "FromGNodeAlias": scada_alias}


async def test_a_live_layout_at_the_fleet_version_sets_the_house_layout():
    store = HouseStateStore()
    updates: list[HouseState] = []

    async def on_update(state: HouseState) -> None:
        updates.append(state)

    body = json.dumps({
        "TypeName": "gw",
        "Header": {
            "TypeName": "gridworks.header",
            "Version": "001",
            "Src": OAK_SCADA,
            "Dst": "ltn",
            "MessageType": "layout.lite",
            "MessageId": "",
            "AckRequired": False,
        },
        "Payload": layout_payload(OAK_SCADA),
    }).encode()
    await handle_message(SemaCodec(), body, store, on_update)
    oak = store.get("oak")
    assert oak is not None and isinstance(oak.layout, LayoutLite)
    assert oak.layout.version == "014"
    assert updates == [oak]


def test_an_unreadable_layouts_row_costs_that_row_alone():
    store = HouseStateStore()
    unreadable = {**layout_payload(BEECH_SCADA), "Version": "999"}
    rows = [(BEECH_SCADA, unreadable), (OAK_SCADA, layout_payload(OAK_SCADA))]
    assert apply_layout_rows(rows, store, SemaCodec()) == 1
    assert store.get("beech") is None
    oak = store.get("oak")
    assert oak is not None and isinstance(oak.layout, LayoutLite)
