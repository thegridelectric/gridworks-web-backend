"""The gateway's one read of the layouts projection, at start.

Each house's current layout is a row of ``gridworks.layouts`` keyed by the
TerminalAsset GNodeId; the gateway reads every row once so a house has
its layout before its scada next sends ``layout.lite``, then keeps the
layout current from the live message. The row's ``from_alias`` is the
scada that sent the layout, the same alias a live message's header
names, so the house keys the same way on both paths.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.sema import SemaCodec
from gateway.state import AnyLayoutLite, HouseStateStore

logger = logging.getLogger(__name__)

LAYOUTS_SQL = text("SELECT from_alias, payload FROM gridworks.layouts")


def apply_layout_rows(
    rows: Iterable[tuple[str, dict]], store: HouseStateStore, codec: SemaCodec
) -> int:
    """Apply each (from_alias, payload) layouts row to the store; the count
    is the rows applied.

    A payload the gateway's snapshot does not read is logged and skipped;
    the live message fills that house in later.
    """
    applied = 0
    for from_alias, payload in rows:
        try:
            layout = codec.from_dict(payload, auto_upgrade=False)
        except ValueError as e:
            logger.warning("layouts row for %r is not read by this gateway; skipped: %s", from_alias, e)
            continue
        if not isinstance(layout, AnyLayoutLite):
            logger.warning(
                "layouts row for %r is %s %s, which this gateway does not read; skipped",
                from_alias,
                layout.type_name,
                layout.version,
            )
            continue
        if store.seed_layout(from_alias, layout) is not None:
            applied += 1
    return applied


async def seed_layouts(tsdb_url: str, store: HouseStateStore, codec: SemaCodec) -> int:
    """Read every layouts row into the store; the count is the rows applied."""
    engine = create_async_engine(tsdb_url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(LAYOUTS_SQL)).all()
    finally:
        await engine.dispose()

    applied = apply_layout_rows(((row.from_alias, row.payload) for row in rows), store, codec)
    logger.info("Seeded %d of %d layouts rows", applied, len(rows))
    return applied
