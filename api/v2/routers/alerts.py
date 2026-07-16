from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from pydantic import BaseModel, model_validator

from api.alert_history import AlertHistoryRow, flatten_alerts_history
from ..dependencies import get_settings, require_sys_admin_username


class MessagesQueryParams(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def check_start_end(self) -> Self:
        if self.start >= self.end:
            raise ValueError("end must be after start")
        return self

router = APIRouter()

@router.get("/api/v2/installations/*/alerts", response_model=list[AlertHistoryRow])
async def get_alert_history(
    query: Annotated[MessagesQueryParams, Query()],
    settings = Depends(get_settings),
    username: str = Depends(require_sys_admin_username)
):
    # Proxy to the alert-manager service so its bearer secret never reaches the browser.
    base = settings.alert_manager_url.rstrip("/")
    token = settings.alert_manager_token.get_secret_value()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base}/alerts-history",
                params={"start": query.start.timestamp(), "end": query.end.timestamp()},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach alert-manager: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"alert-manager returned {resp.status_code}")
    return flatten_alerts_history(resp.json())
    