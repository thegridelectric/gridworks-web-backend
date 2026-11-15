"""The visualizer's price panel: the prices a market cleared at over a
window, served as the `latest.price` words the MarketMaker published."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot
from api.sema.types import LatestPrice

from ..dependencies import get_current_username, get_db
from .weather_forecast import ForecastWindow

from gw_data.db.models import MessageSql

router = APIRouter()

# One hourly price per slot; a window of weeks is under this.
MAX_PRICES = 2000

# The slot length of the market's product, so the slot in force at the
# window start is served too. Every product the fleet clears is hourly;
# the product's slot length is a fact of gw.market.product.name, and a
# word stating it retires this constant.
SLOT_S = 3600


def slot_start_s(market_slot_name: str) -> int:
    """The slot start the name's last segment states (format market.slot.name)."""
    return int(market_slot_name.rsplit(".", 1)[1])


@router.get("/api/v2/markets/{market}/latest-prices", response_model=None)
async def get_latest_prices(
    market: LeftRightDot,
    window: Annotated[ForecastWindow, Query()],
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> list[dict]:
    """The `latest.price` words the maker of `market` published for the slots
    that start from one slot before the window's start through its end,
    oldest slot first, each in the dict form of `latest.price` 001; a 000 row
    is served upgraded, its whole price as one `Unknown` part. `market` is
    the slot name without its slot start, `<commodity>.<product>.<maker alias>`.
    Empty when the market cleared nothing in the window."""
    prefix = f"{market}."
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == LatestPrice.type_name_value(),
            MessageSql.payload["MarketSlotName"].astext.like(f"{prefix}%"),
            MessageSql.timestamp >= window.start - timedelta(seconds=2 * SLOT_S),
            MessageSql.timestamp <= window.end + timedelta(seconds=SLOT_S),
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(MAX_PRICES)
    )
    rows = (await db.execute(db_query)).all()
    start_s = int(window.start.timestamp())
    end_s = int(window.end.timestamp())
    codec = SemaCodec()
    prices = [codec.from_dict(row[0], expect=LatestPrice) for row in rows]
    in_window = [
        p for p in prices
        if slot_start_s(p.market_slot_name) + SLOT_S > start_s and slot_start_s(p.market_slot_name) <= end_s
    ]
    in_window.sort(key=lambda p: slot_start_s(p.market_slot_name))
    return [p.to_dict() for p in in_window]
