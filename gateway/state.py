import re
import time
from dataclasses import dataclass, field
from typing import Optional

THERMOSTAT_CHANNEL_PATTERN = re.compile(
    r"^zone(?P<zone_number>\d+)-(?P<human_name>.*)-(temp|set|state)$"
)
ZONE_HISTORY_RETENTION_SECONDS = 3600


def is_zone_whitewire_channel(channel_name: str) -> bool:
    lowered = channel_name.lower()
    return "zone" in lowered and "whitewire" in lowered


@dataclass
class ZoneChannelHistory:
    timestamps: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


class ZoneWhitewireTracker:
    def __init__(self) -> None:
        self._zones: dict[str, ZoneChannelHistory] = {}

    def update_from_snapshot(self, snapshot: dict) -> None:
        for reading in snapshot.get("LatestReadingList", []):
            channel_name = reading.get("ChannelName")
            if not isinstance(channel_name, str) or not is_zone_whitewire_channel(channel_name):
                continue
            if channel_name not in self._zones:
                self._zones[channel_name] = ZoneChannelHistory()
            history = self._zones[channel_name]
            history.timestamps.append(int(reading["ScadaReadTimeUnixMs"] / 1000))
            history.values.append(float(reading["Value"]))
        self.prune_stale_points()

    def prune_stale_points(self, retention_seconds: int = ZONE_HISTORY_RETENTION_SECONDS) -> None:
        cutoff = int(time.time()) - retention_seconds
        empty_channels: list[str] = []
        for channel_name, history in self._zones.items():
            kept_timestamps: list[int] = []
            kept_values: list[float] = []
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


def thermostat_names_from_layout(layout: dict) -> list[str]:
    names: list[str] = []
    for channel in layout.get("DataChannels", []):
        match = THERMOSTAT_CHANNEL_PATTERN.match(channel.get("Name", ""))
        if match and (human_name := match.group("human_name")) not in names:
            names.append(human_name)
    return names


def system_mode_from_layout(layout: dict | None) -> str | None:
    if layout is None:
        return None
    system_mode = layout.get("SystemMode")
    if system_mode is None:
        return None
    return str(system_mode)


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
    layout: Optional[dict] = None
    snapshot: Optional[dict] = None
    thermostat_names: list[str] = field(default_factory=list)
    zone_tracker: ZoneWhitewireTracker = field(default_factory=ZoneWhitewireTracker)
    messages_received: int = 0
    last_message_time: float = 0.0

    @property
    def snapshot_time_ms(self) -> int:
        if self.snapshot is None:
            return 0
        return int(self.snapshot.get("SnapshotTimeUnixMs", 0))

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
            "message_type": "snapshot.spaceheat",
            "payload": self.snapshot,
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

    def update_layout(self, g_node_alias: str, layout: dict) -> Optional[HouseState]:
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        state.layout = layout
        state.thermostat_names = thermostat_names_from_layout(layout)
        state.messages_received += 1
        state.last_message_time = time.time()
        return state

    def update_snapshot(self, g_node_alias: str, snapshot: dict) -> Optional[HouseState]:
        state = self._get_or_create(g_node_alias)
        if state is None:
            return None
        snapshot_time = int(snapshot.get("SnapshotTimeUnixMs", 0))
        if state.snapshot is not None and snapshot_time <= state.snapshot_time_ms:
            return None
        state.snapshot = snapshot
        state.zone_tracker.update_from_snapshot(snapshot)
        state.messages_received += 1
        state.last_message_time = time.time()
        return state

    def prune_all_zone_trackers(self) -> None:
        for state in self._by_short_alias.values():
            state.zone_tracker.prune_stale_points()
