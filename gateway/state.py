import re
import time
from dataclasses import dataclass, field
from typing import Optional

from gateway.sema.types import LayoutLite, ReportEvent, SnapshotSpaceheat
from gateway.sema.types.old_versions.layout_lite_011 import LayoutLite011
from gateway.sema.types.old_versions.report_event_002 import ReportEvent002
from gateway.sema.types.old_versions.report_event_003 import ReportEvent003

# The versions the fleet sends, each read as its own class.
AnyLayoutLite = LayoutLite | LayoutLite011
AnyReportEvent = ReportEvent | ReportEvent002 | ReportEvent003

THERMOSTAT_CHANNEL_PATTERN = re.compile(
    r"^zone(?P<zone_number>\d+)-(?P<human_name>.*)-(temp|set|state)$"
)
ZONE_HISTORY_RETENTION_SECONDS = 3600


def is_zone_whitewire_channel(channel_name: str) -> bool:
    lowered = channel_name.lower()
    return "zone" in lowered and "whitewire" in lowered


def zone_whitewire_threshold(short_alias: str) -> float:
    lowered = short_alias.lower()
    if "beech" in lowered:
        return 100.0
    if "elm" in lowered:
        return 0.9
    return 20.0


def binarize_whitewire_value(raw_value: float, threshold: float) -> int:
    return 1 if raw_value > threshold else 0


@dataclass
class ZoneChannelHistory:
    timestamps: list[int] = field(default_factory=list)
    values: list[int] = field(default_factory=list)


class ZoneWhitewireTracker:
    def __init__(self) -> None:
        self._zones: dict[str, ZoneChannelHistory] = {}

    def update_from_snapshot(self, snapshot: SnapshotSpaceheat, short_alias: str) -> None:
        threshold = zone_whitewire_threshold(short_alias)
        for reading in snapshot.latest_reading_list:
            if not is_zone_whitewire_channel(reading.channel_name):
                continue
            if reading.channel_name not in self._zones:
                self._zones[reading.channel_name] = ZoneChannelHistory()
            history = self._zones[reading.channel_name]
            history.timestamps.append(int(reading.scada_read_time_unix_ms / 1000))
            history.values.append(
                binarize_whitewire_value(float(reading.value), threshold)
            )
        self.prune_stale_points()

    def prune_stale_points(self, retention_seconds: int = ZONE_HISTORY_RETENTION_SECONDS) -> None:
        cutoff = int(time.time()) - retention_seconds
        empty_channels: list[str] = []
        for channel_name, history in self._zones.items():
            kept_timestamps: list[int] = []
            kept_values: list[int] = []
            for timestamp, value in zip(history.timestamps, history.values):
                if timestamp >= cutoff:
                    kept_timestamps.append(timestamp)
                    kept_values.append(value)
            history.timestamps = kept_timestamps
            history.values = kept_values
            if not kept_timestamps:
                empty_channels.append(channel_name)
        for channel_name in empty_channels:
            del self._zones[channel_name]

    def highest_average_zone_channel(self) -> str | None:
        best_channel: str | None = None
        best_average = float("-inf")
        for channel_name, history in self._zones.items():
            if not history.values:
                continue
            average = sum(history.values) / len(history.values)
            if average > best_average:
                best_average = average
                best_channel = channel_name
        return best_channel

    def zone_whitewire_series_message(self) -> Optional[dict]:
        channel_name = self.highest_average_zone_channel()
        if channel_name is None:
            return None
        history = self._zones[channel_name]
        return {
            "type": "zone_whitewire_series",
            "channel_name": channel_name,
            "timestamps": history.timestamps,
            "values": history.values,
        }


def short_alias_from_gnode(g_node_alias: str) -> str | None:
    parts = g_node_alias.split(".")
    if len(parts) < 2:
        return None
    return parts[-2]


def thermostat_names_from_layout(layout: AnyLayoutLite) -> list[str]:
    names: list[str] = []
    for channel in layout.data_channels:
        match = THERMOSTAT_CHANNEL_PATTERN.match(channel.name)
        if match and (human_name := match.group("human_name")) not in names:
            names.append(human_name)
    return names


def system_mode_from_layout(layout: AnyLayoutLite | None) -> str | None:
    if layout is None:
        return None
    return layout.system_mode.value


def empty_status_message(connected_clients: int) -> dict:
    return {
        "type": "status",
        "mqtt_connected": True,
        "layout_loaded": False,
        "snapshot_loaded": False,
        "target_gnode": "",
        "thermostat_names": [],
        "connected_clients": connected_clients,
    }


@dataclass
class HouseState:
    g_node_alias: str
    short_alias: str
    layout: Optional[AnyLayoutLite] = None
    snapshot: Optional[SnapshotSpaceheat] = None
    report_time_ms: int = 0
    thermostat_names: list[str] = field(default_factory=list)
    zone_tracker: ZoneWhitewireTracker = field(default_factory=ZoneWhitewireTracker)
    messages_received: int = 0
    last_message_time: float = 0.0

    @property
    def snapshot_time_ms(self) -> int:
        if self.snapshot is None:
            return 0
        return int(self.snapshot.snapshot_time_unix_ms)

    def status_message(self, connected_clients: int) -> dict:
        last_activity = "Never"
        if self.last_message_time:
            last_activity = f"{int(time.time() - self.last_message_time)}s ago"
        return {
            "type": "status",
            "mqtt_connected": True,
            "layout_loaded": self.layout is not None,
            "snapshot_loaded": self.snapshot is not None,
            "target_gnode": self.g_node_alias,
            "thermostat_names": self.thermostat_names,
            "system_mode": system_mode_from_layout(self.layout),
            "messages_received": self.messages_received,
            "connected_clients": connected_clients,
            "last_activity": last_activity,
        }

    def snapshot_message(self) -> Optional[dict]:
        if self.snapshot is None:
            return None
        return {
            "type": "mqtt_message",
            "message_type": self.snapshot.type_name,
            "payload": self.snapshot.to_dict(),
        }

    def zone_whitewire_series_message(self) -> Optional[dict]:
        return self.zone_tracker.zone_whitewire_series_message()


class HouseStateStore:
    def __init__(self) -> None:
        self._by_short_alias: dict[str, HouseState] = {}

    def get(self, short_alias: str) -> Optional[HouseState]:
        return self._by_short_alias.get(short_alias)

    def all_houses(self) -> list[HouseState]:
        return list(self._by_short_alias.values())

    def _get_or_create(self, g_node_alias: str) -> Optional[HouseState]:
        short_alias = short_alias_from_gnode(g_node_alias)
        if short_alias is None:
            return None
        state = self._by_short_alias.get(short_alias)
        if state is None:
            state = HouseState(g_node_alias=g_node_alias, short_alias=short_alias)
            self._by_short_alias[short_alias] = state
        return state

    def seed_layout(self, g_node_alias: str, layout: AnyLayoutLite) -> Optional[HouseState]:
        """The house's current layout from the layouts projection at start:
        the same state a live layout.lite sets, with no message counted."""
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        state.layout = layout
        state.thermostat_names = thermostat_names_from_layout(layout)
        return state

    def update_layout(self, g_node_alias: str, layout: AnyLayoutLite) -> Optional[HouseState]:
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        state.layout = layout
        state.thermostat_names = thermostat_names_from_layout(layout)
        state.messages_received += 1
        state.last_message_time = time.time()
        return state

    def update_snapshot(
        self, g_node_alias: str, snapshot: SnapshotSpaceheat
    ) -> Optional[HouseState]:
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        if state.snapshot is not None and snapshot.snapshot_time_unix_ms <= state.snapshot_time_ms:
            return None
        state.snapshot = snapshot
        state.zone_tracker.update_from_snapshot(snapshot, state.short_alias)
        state.messages_received += 1
        state.last_message_time = time.time()
        return state

    def update_report(self, g_node_alias: str, report: AnyReportEvent) -> Optional[HouseState]:
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        state.report_time_ms = int(report.time_created_ms)
        state.messages_received += 1
        state.last_message_time = time.time()
        return state

    def prune_all_zone_trackers(self) -> None:
        for state in self._by_short_alias.values():
            state.zone_tracker.prune_stale_points()
