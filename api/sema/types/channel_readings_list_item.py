from typing import Literal
from pydantic import StrictInt
from api.sema.base import SemaType
from api.sema.property_format import LeftRightDot
from api.sema.property_format import SpaceheatName


class ChannelReadingsListItem(SemaType):
    """Sema: https://schemas.electricity.works/types/channel.readings.list.item/000"""

    channel_name: SpaceheatName
    value_list: list[StrictInt | None]
    unit: str
    unit_type: LeftRightDot
    type_name: Literal["channel.readings.list.item"] = "channel.readings.list.item"
    version: Literal["000"] = "000"
