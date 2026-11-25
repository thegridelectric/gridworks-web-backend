"""The gateway's seed from the layouts projection: every row the snapshot
reads becomes its house's layout, keyed the way a live layout.lite keys it,
with no message counted; a row at an older version is skipped."""

from gateway.layouts import seed_layouts
from gateway.sema import SemaCodec
from gateway.sema.types.old_versions.layout_lite_013 import LayoutLite013
from gateway.state import HouseStateStore

from .conftest import async_url


async def test_gateway_seeds_the_layouts_rows_it_reads(pg_url):
    """The seeded database has oak at 013 and beech at 012, a version the
    gateway's snapshot does not have."""
    store = HouseStateStore()
    applied = await seed_layouts(async_url(pg_url), store, SemaCodec())
    assert applied == 1
    assert store.get("beech") is None
    oak = store.get("oak")
    assert oak is not None
    assert isinstance(oak.layout, LayoutLite013)
    assert oak.g_node_alias.endswith("oak.scada")
    assert oak.status_message(0)["layout_loaded"] is True
    assert oak.messages_received == 0
