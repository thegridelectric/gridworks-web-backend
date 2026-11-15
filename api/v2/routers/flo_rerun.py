"""The FLO re-run: a `flo.params` word in, solved on this machine by the
bruce package, a `flo.recommendation` out, so the visualizer draws a solve
with the tuning changed the way it draws the journaled one. Mounted only
where the settings turn it on (`BACKEND_FLO_RERUN=in-process`)."""
import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.base import SemaError
from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot
from api.sema.types import FloRecommendation

from ..dependencies import get_current_username, get_db
from ..shared_queries import verify_data_access
from .flo_recommendation import JOURNALED_PARAMS

router = APIRouter()

NOT_THIS_HOUSE = "The flo.params names another house"


def mount(app: FastAPI, flo_rerun: Literal["off", "in-process"]) -> None:
    """Adds the route to `app` when the setting turns it on; the solver's
    import runs here so an API turned on without bruce fails at start."""
    if flo_rerun == "off":
        return
    import bruce.service  # noqa: F401

    app.include_router(router)


def solve(params: dict) -> dict:
    """bruce's recommendation for one `flo.params`, both in dict form: the
    word crosses into bruce's own snapshot and the answer crosses back."""
    from bruce.sema.types import FloParams as BruceFloParams
    from bruce.service import recommend

    return recommend(BruceFloParams.from_dict(params)).to_dict()


@router.post("/api/v2/installations/{installation_id}/flo.rerun", response_model=None)
async def rerun(
    installation_id: LeftRightDot,
    body: dict = Body(),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """Solves the `flo.params` word in the body and answers the
    `flo.recommendation` 001 bruce gives, in dict form. The word is the
    hour's journaled request, at either version the journal holds, with its
    Tuning edited, and its GNodeAlias is the house in the path. A 422 says
    what bruce could not read or solve."""
    try:
        params = SemaCodec().from_dict(body, auto_upgrade=False)
    except (SemaError, ValueError, ValidationError) as e:
        raise HTTPException(status_code=422, detail=f"Not a flo.params: {e}") from e
    if not isinstance(params, JOURNALED_PARAMS):
        raise HTTPException(status_code=422, detail=f"Not a flo.params: {type(params).__name__}")
    if params.g_node_alias != installation_id:
        raise HTTPException(status_code=400, detail=NOT_THIS_HOUSE)
    await verify_data_access(
        db, username, installation_id, effective_date=datetime.fromtimestamp(params.start_unix_s, tz=UTC)
    )
    try:
        answer = await asyncio.to_thread(solve, params.to_dict())
    except (ValueError, ValidationError, RuntimeError) as e:
        raise HTTPException(status_code=422, detail=f"bruce could not solve the flo.params: {e}") from e
    return SemaCodec().from_dict(answer, expect=FloRecommendation).to_dict()
