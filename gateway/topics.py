from dataclasses import dataclass


@dataclass(frozen=True)
class DecodedRoutingKey:
    envelope_type: str
    src: str  # gnode alias, e.g. hw1.isone.me.versant.keene.oak.scada
    dst: str
    message_type: str  # message type, e.g. snapshot.spaceheat


def decode_routing_key(routing_key: str) -> DecodedRoutingKey | None:
    """Decode an AMQP routing key into its gw-topic components."""
    parts = routing_key.split(".")
    if len(parts) < 5 or parts[2] != "to":
        return None
    return DecodedRoutingKey(
        envelope_type=parts[0],
        src=parts[1].replace("-", "."),
        dst=parts[3].replace("-", "."),
        message_type=parts[4].replace("-", "."),
    )


def short_alias_from_gnode(g_node_alias: str) -> str | None:
    """hw1.isone.me.versant.keene.oak.scada -> oak"""
    parts = g_node_alias.split(".")
    if len(parts) < 2:
        return None
    return parts[-2]
