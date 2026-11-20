"""The FLO re-run: a `flo.rerun.request` word in, solved on this machine by
the bruce package, a `flo.recommendation` out, so the visualizer draws a
solve with the tuning and the forced heat-pump steps changed the way it
draws the journaled one. Mounted only where the settings turn it on
(`BACKEND_FLO_RERUN=in-process`)."""
import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.sema.base import SemaError
from api.sema.codec import SemaCodec
from api.sema.property_format import LeftRightDot
from api.sema.types import FloRecommendation, FloRerunRequest
from api.sema.types.old_versions.flo_rerun_request_001 import FloRerunRequest001

from ..dependencies import get_current_username, get_db
from ..shared_queries import verify_data_access

router = APIRouter()

NOT_THIS_HOUSE = "The flo.rerun.request names another house"
NOT_LOCALLY = "BACKEND_FLO_RERUN=in-process is for an API running locally (BACKEND_RUNNING_LOCALLY=true)"


def mount(app: FastAPI, flo_rerun: Literal["off", "in-process"], running_locally: bool) -> None:
    """Adds the route to `app` when the setting turns it on. The re-run is
    a developer tool, so an API that is not running locally refuses to
    start with it on, and the solver's import runs here so an API turned
    on without bruce fails at start too."""
    if flo_rerun == "off":
        return
    if not running_locally:
        raise RuntimeError(NOT_LOCALLY)
    import bruce.service  # noqa: F401

    app.include_router(router)


def solve(body: dict) -> dict:
    """bruce's recommendation for one `flo.rerun.request`, both in dict
    form: the word crosses into bruce's own snapshot and the answer crosses
    back. A word bruce cannot read or solve raises with bruce's reason."""
    from bruce.sema.base import SemaError as BruceSemaError
    from bruce.sema.types import FloRerunRequest as BruceFloRerunRequest
    from bruce.service import recommend

    try:
        request = BruceFloRerunRequest.from_dict(body)
    except BruceSemaError as e:
        raise ValueError(f"bruce could not read the flo.rerun.request: {e}") from e
    return recommend(request).to_dict()


@router.post("/api/v2/installations/{installation_id}/flo.rerun", response_model=None)
async def rerun(
    installation_id: LeftRightDot,
    body: dict = Body(),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username),
) -> dict:
    """Solves the `flo.rerun.request` in the body and answers the
    `flo.recommendation` 003 bruce gives, in dict form. The word wraps the
    hour's `flo.params` (its Tuning possibly edited, which lifts an older
    version to 004) with the heat-pump steps to force on or off, and that params'
    GNodeAlias is the house in the path. A 422 says what bruce could not
    read or solve: an hour whose params are at a version bruce no longer
    reads answers 422."""
    try:
        request = SemaCodec().from_dict(body, auto_upgrade=False)
    except (SemaError, ValueError, ValidationError) as e:
        raise HTTPException(status_code=422, detail=f"Not a flo.rerun.request: {e}") from e
    if not isinstance(request, (FloRerunRequest, FloRerunRequest001)):
        raise HTTPException(
            status_code=422, detail=f"Not a flo.rerun.request: {type(request).__name__}"
        )
    params = request.params
    if params.g_node_alias != installation_id:
        raise HTTPException(status_code=400, detail=NOT_THIS_HOUSE)
    await verify_data_access(
        db, username, installation_id, effective_date=datetime.fromtimestamp(params.start_unix_s, tz=UTC)
    )
    try:
        answer = await asyncio.to_thread(solve, request.to_dict())
    except (ValueError, ValidationError, RuntimeError) as e:
        raise HTTPException(status_code=422, detail=f"bruce could not solve the flo.rerun.request: {e}") from e
    return SemaCodec().from_dict(answer, expect=FloRecommendation).to_dict()
