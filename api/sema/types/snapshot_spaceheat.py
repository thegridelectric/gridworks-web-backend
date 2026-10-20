from typing import Literal
from api.sema.base import SemaType
from api.sema.property_format import LeftRightDot
from api.sema.property_format import UTCMilliseconds
from api.sema.property_format import UUID4Str
from api.sema.types.single_machine_state import SingleMachineState
from api.sema.types.single_reading import SingleReading


class SnapshotSpaceheat(SemaType):
    """Sema: https://schemas.electricity.works/types/snapshot.spaceheat/003"""

    from_g_node_alias: LeftRightDot
    from_g_node_instance_id: UUID4Str
    snapshot_time_unix_ms: UTCMilliseconds
    latest_reading_list: list[SingleReading]
    latest_state_list: list[SingleMachineState]
    type_name: Literal["snapshot.spaceheat"] = "snapshot.spaceheat"
    version: Literal["003"] = "003"
