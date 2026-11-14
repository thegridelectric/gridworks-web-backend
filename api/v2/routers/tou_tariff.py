"""The visualizer's on-peak shading: the fleet's time-of-use tariff, served
as the `gw.tou.tariff` word.

Every house in the fleet settles delivery under Versant Rate A-1, so the
route serves that one tariff; once houses journal their
`gw.operational.params`, the route serves the house's own `Tariff`.
"""

from fastapi import APIRouter, Depends

from api.sema.enums import DayOfWeek
from api.sema.types import TouTariff, TouWindow

from ..dependencies import get_current_username

router = APIRouter()

WEEKDAYS = [
    DayOfWeek.Monday,
    DayOfWeek.Tuesday,
    DayOfWeek.Wednesday,
    DayOfWeek.Thursday,
    DayOfWeek.Friday,
]

FLEET_TARIFF = TouTariff(
    alias="versant.a1.home.eco.bonus",
    display_name="Home Eco Rate with Bonus Meter",
    timezone_str="America/New_York",
    on_peak_windows=[
        TouWindow(start="07:00", end="12:00", days=WEEKDAYS),
        TouWindow(start="16:00", end="20:00", days=WEEKDAYS),
    ],
)


@router.get("/api/v2/tou-tariff/current", response_model=None)
async def get_current_tou_tariff(
    username: str = Depends(get_current_username),
) -> dict:
    """The fleet's time-of-use tariff as a `gw.tou.tariff` word: the
    on-peak windows a reader lays over its own time range."""
    return FLEET_TARIFF.to_dict()
