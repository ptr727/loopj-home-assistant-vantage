"""Forward Vantage triggers, the object that started each task, to the event bus."""

import asyncio
from typing import Any

from aiovantage.config_client import TriggerType
from aiovantage.events import TriggerReceived

from homeassistant.core import HomeAssistant

from .config_entry import VantageConfigEntry
from .const import EVENT_TRIGGER, LOGGER
from .names import ObjectNames


def trigger_type_name(event: int | None) -> str:
    """Return the snake case name of a trigger type, eg. "out_of_range"."""
    try:
        return TriggerType(event).name.lower()  # type: ignore[arg-type]
    except ValueError:
        return f"unknown_{event}"


async def describe_trigger(
    names: ObjectNames,
    source: int,
    destination: int,
    event: int | None,
    param1: int | None,
    param2: int | None,
) -> dict[str, Any]:
    """Build the event payload, and service response entry, for a trigger."""
    source_path = await names.path(source)
    return {
        "trigger_type": trigger_type_name(event),
        "event": event,
        "source_id": source,
        "source_name": source_path[0] if source_path else f"VID {source}",
        "source_path": source_path,
        "task_id": destination,
        "task_name": await names.name(destination),
        "param1": param1,
        "param2": param2,
    }


async def async_setup_triggers(hass: HomeAssistant, entry: VantageConfigEntry) -> None:
    """Fire an event each time an object triggers a task."""
    vantage = entry.runtime_data.client
    names = entry.runtime_data.object_names

    # Triggers are only reported through the enhanced log
    conn = await vantage.event_stream.start()
    if not conn.supports_enhanced_log:
        LOGGER.info("Controller firmware does not report triggers")
        return

    # Fire in the order the controller reported, though some names need a lookup
    queue: asyncio.Queue[TriggerReceived] = asyncio.Queue()

    async def fire_in_order() -> None:
        while True:
            trigger = await queue.get()
            payload = await describe_trigger(
                names,
                trigger.source,
                trigger.destination,
                trigger.event,
                trigger.param1,
                trigger.param2,
            )
            hass.bus.async_fire(EVENT_TRIGGER, {"host": vantage.host} | payload)

    entry.async_create_background_task(hass, fire_in_order(), "vantage_triggers")
    entry.async_on_unload(
        vantage.event_stream.subscribe_triggers(queue.put_nowait, peers=vantage.peers)
    )
