"""The visualizer's FLO view: for a house's market slot, the request the
LTN sent and the recommendation the FLO service answered with, each served
as the word, at the version the journal holds."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot
from api.sema.types import FloParams, FloRecommendation
from api.sema.types.old_versions.flo_recommendation_000 import FloRecommendation000

from ..dependencies import get_current_username, get_db
from ..shared_queries import verify_data_access

from gw_data.db.models import MessageSql

router = APIRouter()

# The market's slot length; slots start on the hour. A fact about the
# market the house bids into, which no word states yet.
MARKET_SLOT_S = 3600

# The versions the journal holds: 000 rows have no store state and no
# penalties, so each is served at its own version.
JOURNALED_RECOMMENDATION = (FloRecommendation, FloRecommendation000)
JOURNALED_PARAMS = (FloParams,)

NO_FLO_RAN = "No FLO was run in this hour"


def slot_start_s(time: datetime) -> int:
    return int(time.timestamp()) // MARKET_SLOT_S * MARKET_SLOT_S


async def newest_slot_word(
    db: AsyncSession, type_name: str, bidder: ColumnElement, slot_start: ColumnElement,
    installation_id: str, slot_s: int, journaled: tuple[type, ...],
):
    """The newest journal row of `type_name` whose bidder is the house and
    whose slot starts at `slot_s`, decoded at the version the row states;
    a 404 when the slot has none."""
    db_query = (
        select(MessageSql.payload)
        .filter(
            MessageSql.message_type_name == type_name,
            bidder.astext == installation_id,
            slot_start.astext == str(slot_s),
        )
        .order_by(MessageSql.timestamp.desc())
        .limit(1)
    )
    row = (await db.execute(db_query)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=NO_FLO_RAN)
    word = SemaCodec().from_dict(row[0], auto_upgrade=False)
    if not isinstance(word, journaled):
        raise HTTPException(status_code=500, detail=f"journal row decoded as {type(word).__name__}")
    return word


@router.get("/api/v2/installations/{installation_id}/flo.recommendation", response_model=None)
async def get_recommendation(
    installation_id: LeftRightDot,
    time: datetime = Query(),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """The newest `flo.recommendation` the journal holds for this house and
    the market slot `time` falls in, in the word's dict form: the bid
    pairs and, per pair, the optimal path and, at 001, the store state and
    the optimizer's penalties. A 404 when no FLO ran for that slot."""
    await verify_data_access(db, username, installation_id, effective_date=time)
    recommendation = await newest_slot_word(
        db, FloRecommendation.type_name_value(),
        MessageSql.payload["BidderAlias"], MessageSql.payload["OptimalPathList"][0]["StartUnixS"],
        installation_id, slot_start_s(time), JOURNALED_RECOMMENDATION,
    )
    return recommendation.to_dict()


@router.get("/api/v2/installations/{installation_id}/flo.params", response_model=None)
async def get_params(
    installation_id: LeftRightDot,
    time: datetime = Query(),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """The newest `flo.params` the journal holds for this house and the
    market slot `time` falls in, in the word's dict form: the step grid,
    the price, weather and load forecasts, the store and heat pump facts
    and the optimizer's tuning. A 404 when the LTN sent no request for
    that slot."""
    await verify_data_access(db, username, installation_id, effective_date=time)
    params = await newest_slot_word(
        db, FloParams.type_name_value(),
        MessageSql.payload["GNodeAlias"], MessageSql.payload["StartUnixS"],
        installation_id, slot_start_s(time), JOURNALED_PARAMS,
    )
    return params.to_dict()
