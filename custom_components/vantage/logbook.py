"""Describe Vantage events in the logbook."""

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_SYSTEM_LOG, EVENT_TRIGGER


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[Event[Any]], dict[str, str]]], None
    ],
) -> None:
    """Describe Vantage triggers, eg. "Out of range: Power Sensor - Line C, Module 3"."""

    @callback
    def describe_trigger(event: Event[Any]) -> dict[str, str]:
        data = event.data
        trigger = str(data.get("trigger_type", "")).replace("_", " ").capitalize()
        source = ", ".join(data.get("source_path") or [str(data.get("source_name"))])
        return {
            LOGBOOK_ENTRY_NAME: "Vantage",
            LOGBOOK_ENTRY_MESSAGE: f"{trigger}: {source} started {data.get('task_name')}",
        }

    @callback
    def describe_system_log(event: Event[Any]) -> dict[str, str]:
        data = event.data
        return {
            LOGBOOK_ENTRY_NAME: "Vantage",
            LOGBOOK_ENTRY_MESSAGE: f"{str(data.get('type', '')).capitalize()}: {data.get('message')}",
        }

    async_describe_event(DOMAIN, EVENT_TRIGGER, describe_trigger)
    async_describe_event(DOMAIN, EVENT_SYSTEM_LOG, describe_system_log)
