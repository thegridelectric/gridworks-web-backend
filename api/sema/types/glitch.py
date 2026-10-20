from typing import Literal
from api.sema.base import SemaType
from api.sema.enums import LogLevel
from api.sema.property_format import LeftRightDot
from api.sema.property_format import SpaceheatName
from api.sema.property_format import UTCMilliseconds


class Glitch(SemaType):
    """Sema: https://schemas.electricity.works/types/glitch/000"""

    from_g_node_alias: LeftRightDot
    node: SpaceheatName
    type: LogLevel
    summary: str
    details: str
    created_ms: UTCMilliseconds
    type_name: Literal["glitch"] = "glitch"
    version: Literal["000"] = "000"
