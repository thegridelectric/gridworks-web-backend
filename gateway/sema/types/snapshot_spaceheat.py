from typing import Literal
from gateway.sema.base import SemaType
from gateway.sema.property_format import LeftRightDot
from gateway.sema.property_format import UTCMilliseconds
from gateway.sema.property_format import UUID4Str
from gateway.sema.types.single_machine_state import SingleMachineState
from gateway.sema.types.single_reading import SingleReading


class SnapshotSpaceheat(SemaType):
    """Sema: https://schemas.electricity.works/types/snapshot.spaceheat/003"""

    from_g_node_alias: LeftRightDot
    from_g_node_instance_id: UUID4Str
    snapshot_time_unix_ms: UTCMilliseconds
    latest_reading_list: list[SingleReading]
    latest_state_list: list[SingleMachineState]
    type_name: Literal["snapshot.spaceheat"] = "snapshot.spaceheat"
    version: Literal["003"] = "003"
