"""Forward Vantage triggers, the object that started each task, to the event bus."""

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

    # Triggers are only reported through the enhanced log
    conn = await vantage.event_stream.start()
    if not conn.supports_enhanced_log:
        LOGGER.info("Controller firmware does not report triggers")
        return
    names = entry.runtime_data.object_names

    async def fire(trigger: TriggerReceived) -> None:
        payload = await describe_trigger(
            names,
            trigger.source,
            trigger.destination,
            trigger.event,
            trigger.param1,
            trigger.param2,
        )
        hass.bus.async_fire(EVENT_TRIGGER, payload)

    def on_trigger(trigger: TriggerReceived) -> None:
        # Names of objects that aren't loaded may need a lookup on the controller
        entry.async_create_background_task(hass, fire(trigger), "vantage_trigger")

    entry.async_on_unload(
        vantage.event_stream.subscribe_triggers(on_trigger, peers=vantage.peers)
    )
