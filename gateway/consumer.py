import asyncio
import json
import logging
import uuid
from typing import Awaitable, Callable

import aio_pika

from gateway.config import EAR_TX, FLEET_BINDING_KEY, QUEUE_PREFIX, GatewaySettings
from gateway.sema import SemaCodec
from gateway.sema.types import Gw, LayoutLite, ReportEvent, SnapshotSpaceheat
from gateway.state import AnyLayoutLite, AnyReportEvent, HouseState, HouseStateStore

logger = logging.getLogger(__name__)

SCADA_SUFFIX = ".scada"
HANDLED_TYPE_NAMES = {
    LayoutLite.type_name_value(),
    SnapshotSpaceheat.type_name_value(),
    ReportEvent.type_name_value(),
}

HouseUpdateCallback = Callable[[HouseState], Awaitable[None]]


async def consume(
    settings: GatewaySettings,
    store: HouseStateStore,
    on_house_update: HouseUpdateCallback,
) -> None:
    codec = SemaCodec()
    connection = await aio_pika.connect_robust(settings.rabbit_url.get_secret_value())
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=100)
        exchange = await channel.get_exchange(EAR_TX, ensure=False)
        queue = await channel.declare_queue(f"{QUEUE_PREFIX}{uuid.uuid4()}", exclusive=True)
        await queue.bind(exchange, routing_key=FLEET_BINDING_KEY)
        logger.info(
            "Consuming %r on %r through queue %r",
            FLEET_BINDING_KEY,
            EAR_TX,
            queue.name,
        )
        async with queue.iterator() as messages:
            async for message in messages:
                async with message.process():
                    try:
                        await handle_message(codec, message.body, store, on_house_update)
                    except Exception:
                        logger.exception(
                            "Error handling message with routing key %r",
                            message.routing_key,
                        )


async def handle_message(
    codec: SemaCodec,
    body: bytes,
    store: HouseStateStore,
    on_house_update: HouseUpdateCallback,
) -> None:
    """Decode one fleet message and apply it to its house's state.

    Each payload is read at the version it was sent in (no upgrade), so a
    house on an older layout.lite keeps the fields that version has.
    """
    envelope = codec.from_dict(json.loads(body), expect=Gw)
    if not envelope.header.src.endswith(SCADA_SUFFIX):
        return
    if envelope.payload.type_name not in HANDLED_TYPE_NAMES:
        return

    payload = codec.from_dict(envelope.payload.model_dump(by_alias=True), auto_upgrade=False)
    if isinstance(payload, AnyLayoutLite):
        state = store.update_layout(envelope.header.src, payload)
    elif isinstance(payload, SnapshotSpaceheat):
        state = store.update_snapshot(envelope.header.src, payload)
    elif isinstance(payload, AnyReportEvent):
        state = store.update_report(envelope.header.src, payload)
    else:
        return
    if state is not None:
        await on_house_update(state)


async def run_consumer_forever(
    settings: GatewaySettings,
    store: HouseStateStore,
    on_house_update: HouseUpdateCallback,
) -> None:
    while True:
        try:
            await consume(settings, store, on_house_update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Consumer failed; retrying in 5s")
            await asyncio.sleep(5)
