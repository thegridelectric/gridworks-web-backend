from typing import Literal
from gateway.sema.base import SemaType
from gateway.sema.property_format import LeftRightDot
from gateway.sema.property_format import PositiveInt
from gateway.sema.property_format import UTCMilliseconds
from gateway.sema.property_format import UTCSeconds
from gateway.sema.property_format import UUID4Str
from gateway.sema.types.channel_readings import ChannelReadings
from gateway.sema.types.fsm_full_report import FsmFullReport
from gateway.sema.types.machine_states import MachineStates
from gateway.sema.types.old_versions.fsm_full_report_000 import FsmFullReport000
from gateway.sema.types.report import Report


class Report002(SemaType):
    """Sema: https://schemas.electricity.works/types/report/002"""

    from_g_node_alias: LeftRightDot
    from_g_node_instance_id: UUID4Str
    about_g_node_alias: LeftRightDot
    slot_start_unix_s: UTCSeconds
    slot_duration_s: PositiveInt
    channel_reading_list: list[ChannelReadings]
    state_list: list[MachineStates]
    fsm_report_list: list[FsmFullReport000 | FsmFullReport]
    message_created_ms: UTCMilliseconds
    id: UUID4Str
    type_name: Literal["report"] = "report"
    version: str = "002"

    def upgrade(self) -> Report:
        """- FsmReportList[]: fsm.full.report:000 -> 001"""
        data = self.model_dump()
        data["fsm_report_list"] = [
            fsm_report.upgrade() if fsm_report.version == "000" else fsm_report
            for fsm_report in self.fsm_report_list
        ]
        data["version"] = "003"
        return Report.model_validate(data)
