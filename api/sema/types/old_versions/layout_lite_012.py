from typing import Literal
from pydantic import model_validator
from api.sema.base import SemaType
from api.sema.enums import Gw1SeasonalStorageMode
from api.sema.enums import Gw1SystemMode
from api.sema.property_format import LeftRightDot
from api.sema.property_format import PositiveInt
from api.sema.property_format import UTCMilliseconds
from api.sema.property_format import UUID4Str
from api.sema.types.gw1_tank_temp_calibration_map import Gw1TankTempCalibrationMap
from api.sema.types.ha1_params import Ha1Params
from api.sema.types.i2c_multichannel_dt_relay_component_gt import (
    I2cMultichannelDtRelayComponentGt,
)
from api.sema.types.layout_lite import LayoutLite
from api.sema.types.old_versions.data_channel_gt_002 import DataChannelGt002
from api.sema.types.old_versions.derived_channel_gt_001 import DerivedChannelGt001
from api.sema.types.old_versions.spaceheat_node_gt_301 import SpaceheatNodeGt301
from api.sema.types.pico_flow_module_component_gt import PicoFlowModuleComponentGt
from api.sema.types.pico_tank_module_component_gt import PicoTankModuleComponentGt
from api.sema.types.sim_pico_tank_module_component_gt import (
    SimPicoTankModuleComponentGt,
)


class LayoutLite012(SemaType):
    """Sema: https://schemas.electricity.works/types/layout.lite/012"""

    from_g_node_alias: LeftRightDot
    message_created_ms: UTCMilliseconds
    message_id: UUID4Str
    strategy: str
    system_mode: Gw1SystemMode
    seasonal_storage_mode: Gw1SeasonalStorageMode
    buffer_short_cycling: bool
    zone_list: list[str]
    critical_zone_list: list[str]
    total_store_tanks: PositiveInt
    sh_nodes: list[SpaceheatNodeGt301]
    data_channels: list[DataChannelGt002]
    derived_channels: list[DerivedChannelGt001]
    tank_module_components: list[
        PicoTankModuleComponentGt | SimPicoTankModuleComponentGt
    ]
    flow_module_components: list[PicoFlowModuleComponentGt]
    ha1_params: Ha1Params
    i2c_relay_component: I2cMultichannelDtRelayComponentGt | None = None
    t_map: Gw1TankTempCalibrationMap | None = None
    type_name: Literal["layout.lite"] = "layout.lite"
    version: Literal["012"] = "012"

    @model_validator(mode="after")
    def check_axiom_1(self) -> "LayoutLite012":
        """
        Axiom 1: DcNodeConsistency
        Every DataChannels.AboutNodeName and DataChannels.CapturedByNodeName SHALL reference an
        existing ShNodes.Name, and every captured-by node SHALL have an active ActorClass.
        """
        node_names = {node.name for node in self.sh_nodes}
        active_actorless = {"NoActor"}
        for channel in self.data_channels:
            if (
                channel.about_node_name not in node_names
                or channel.captured_by_node_name not in node_names
            ):
                raise ValueError(
                    "Axiom 1 failed: data channel node references must exist in sh_nodes."
                )
            captured = next(
                node
                for node in self.sh_nodes
                if node.name == channel.captured_by_node_name
            )
            if str(captured.actor_class) in active_actorless:
                raise ValueError(
                    "Axiom 1 failed: captured-by node must have an active actor class."
                )
        return self

    @model_validator(mode="after")
    def check_axiom_2(self) -> "LayoutLite012":
        """
        Axiom 2: NodeHandleHierarchyConsistency
        Every ShNode with a dotted handle SHALL have its immediate boss present as another
        ShNode in the same payload.
        """
        node_names = {node.name for node in self.sh_nodes}
        for node in self.sh_nodes:
            if node.handle and "." in node.handle:
                immediate_boss = node.handle.split(".")[-2]
                if immediate_boss not in node_names:
                    raise ValueError(
                        "Axiom 2 failed: missing immediate boss node for handle hierarchy."
                    )
        return self

    @model_validator(mode="after")
    def check_axiom_3(self) -> "LayoutLite012":
        """
        Axiom 3: CriticalZoneSubset
        CriticalZoneList SHALL be a subset of ZoneList.
        """
        if not set(self.critical_zone_list).issubset(set(self.zone_list)):
            raise ValueError(
                "Axiom 3 failed: critical_zone_list must be a subset of zone_list."
            )
        return self

    @model_validator(mode="after")
    def check_axiom_4(self) -> "LayoutLite012":
        """
        Axiom 4: DerivedNodeConsistency
        Every DerivedChannels.CreatedByNodeName SHALL reference an existing ShNodes.Name whose
        ActorClass is active.
        """
        nodes = {node.name: node for node in self.sh_nodes}
        for channel in self.derived_channels:
            created_by = nodes.get(channel.created_by_node_name)
            if created_by is None or str(created_by.actor_class) == "NoActor":
                raise ValueError(
                    "Axiom 4 failed: derived channel created_by_node_name must reference an active node."
                )
        return self

    def upgrade(self) -> "LayoutLite":
        """
        - ShNodes: spaceheat.node.gt:301 -> 303
        - DataChannels: data.channel.gt:002 -> 004
        - TankModuleComponents, FlowModuleComponents and TMap leave: no consumer reads the pico components or the calibration map off the projection; the hardware layout is their source.
        - I2cRelayComponent leaves: the relay component is read off the hardware layout, and the projection has no reader for it.
        - SystemMode and SeasonalStorageMode leave: the posture facts (Standby, StandbyPosture, SeasonalStorageMode, ServiceMode, AcceptsDispatch, DispatchRefusalReason) ride gw.house.operating.status, sent after the startup announcements and on change, so the LTN has one source for them.
        - Strategy (free manifold-variant string) -> HardwareLayoutTypeName (left.right.dot): the loaded layout word's TypeName is the family; a format, not an enum of layout words, so this word publishes independently of the layout words' status.
        - BufferShortCycling, Ha1Params and TotalStoreTanks leave: operational parameters are not layout; the LTN gets them from its own settings and its own hardware layout until it holds the ops word, and a layout with no store tanks cannot fill a positive tank count.
        - MessageCreatedMs -> CreatedMs: the sender-clock name every word takes by default.
        - StateMachines: one gw.state.machine per machine the running scada hosts (hosting node Name, state enum, enum version), so a report's MachineHandle resolves by its last dotted segment to a declared, versioned enum.
        """
        raise SemaType.upgrade_requires_context(
            "LayoutLite012 cannot be upgraded to "
            "LayoutLite without the running scada's context: "
            "StateMachines is declared from the machines the scada hosts, which a standalone "
            "projection does not carry."
        )
