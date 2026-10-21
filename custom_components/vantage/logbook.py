"""Describe Vantage events in the logbook."""

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_SYSTEM_LOG


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[Event[Any]], dict[str, str]]], None
    ],
) -> None:
    """Describe notable system logs, eg. "Warning: Overload (VID:120)"."""

    @callback
    def describe_system_log(event: Event[Any]) -> dict[str, str]:
        data = event.data
        log_type = str(data.get("type") or "").capitalize()
        message = str(data.get("message") or "")
        return {
            LOGBOOK_ENTRY_NAME: "Vantage",
            LOGBOOK_ENTRY_MESSAGE: f"{log_type}: {message}" if log_type else message,
        }

    async_describe_event(DOMAIN, EVENT_SYSTEM_LOG, describe_system_log)
