"""Map alert-manager history responses to the flat shape exposed by the web API."""

from pydantic import BaseModel


class AlertHistoryRow(BaseModel):
    time_sent: int
    site_alias: str
    alert_alias: str
    message: str
    state: str


class _AlertManagerAlert(BaseModel):
    site_alias: str
    alert_alias: str
    time_sent: int
    message: str


class _AlertManagerTrackedAlert(BaseModel):
    alert: _AlertManagerAlert
    state: str
    count: int = 1
    sends: list[object] = []


def flatten_alerts_history(raw: list[object]) -> list[AlertHistoryRow]:
    rows: list[AlertHistoryRow] = []
    for item in raw:
        tracked = _AlertManagerTrackedAlert.model_validate(item)
        rows.append(
            AlertHistoryRow(
                time_sent=tracked.alert.time_sent,
                site_alias=tracked.alert.site_alias,
                alert_alias=tracked.alert.alert_alias,
                message=tracked.alert.message,
                state=tracked.state,
            )
        )
    return rows
