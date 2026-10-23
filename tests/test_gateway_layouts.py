"""The gateway's seed from the layouts projection: every row becomes its
house's layout, keyed the way a live layout.lite keys it, with no message
counted."""

from gateway.layouts import seed_layouts
from gateway.sema import SemaCodec
from gateway.sema.types import LayoutLite
from gateway.sema.types.old_versions.layout_lite_012 import LayoutLite012
from gateway.state import HouseStateStore

from .conftest import Seed, async_url


async def test_gateway_seeds_every_layouts_row(pg_url):
    store = HouseStateStore()
    applied = await seed_layouts(async_url(pg_url), store, SemaCodec())
    assert applied == len(Seed.houses)
    assert {h.short_alias for h in store.all_houses()} == {alias.split(".")[-1] for alias in Seed.houses}
    beech, oak = store.get("beech"), store.get("oak")
    assert beech is not None and oak is not None
    assert isinstance(beech.layout, LayoutLite012)
    assert beech.g_node_alias.endswith("beech.scada")
    assert isinstance(oak.layout, LayoutLite) and oak.layout.version == "013"
    assert oak.status_message(0)["system_mode"] is None
    assert beech.status_message(0)["system_mode"] == "Heating"
    assert beech.status_message(0)["layout_loaded"] is True
    assert beech.messages_received == 0
