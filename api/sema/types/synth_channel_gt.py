from typing import Literal
from api.sema.base import SemaType
from api.sema.enums import SpaceheatTelemetryName
from api.sema.property_format import LeftRightDot
from api.sema.property_format import PositiveInt
from api.sema.property_format import SpaceheatName
from api.sema.property_format import UUID4Str


class SynthChannelGt(SemaType):
    """Sema: https://schemas.electricity.works/types/synth.channel.gt/000"""

    id: UUID4Str
    name: SpaceheatName
    created_by_node_name: SpaceheatName
    telemetry_name: SpaceheatTelemetryName
    terminal_asset_alias: LeftRightDot
    strategy: str
    display_name: str
    sync_report_minutes: PositiveInt
    type_name: Literal["synth.channel.gt"] = "synth.channel.gt"
    version: Literal["000"] = "000"
