"""Forward notable controller system logs, eg. a module losing power, to the event bus."""

import re
import time

from aiovantage.config_client import SysLogType
from aiovantage.events import SystemLogReceived

from homeassistant.core import HomeAssistant, callback

from .config_entry import VantageConfigEntry
from .const import EVENT_SYSTEM_LOG, LOGGER
from .ordered import OrderedWorker

# Routine information, eg. task and query completions, is too chatty to forward
NOTABLE_TYPES = {SysLogType.WARNING, SysLogType.ERROR, SysLogType.FATAL}

# Module bus changes are logged as information, eg. "Module Offline: 1.9 [120]"
MODULE_LOG = re.compile(r"^Module (Offline|Online|Status)\b")
MODULE_BUS_CHANGE = re.compile(r"^Module (Offline|Online)\b")

# The object a log is about, eg. "(VID:120)" or "[120]"
OBJECT_VID = re.compile(r"\(VID:(\d+)\)|\[(\d+)\]")

# A program write compiles the controller's scripts, and their warnings are not faults
COMPILER_SOURCE = "Compiler"
SCRIPT_COMPILED = re.compile(r" compiled with warnings in [\d.]+ seconds\.?$")

# A warning can repeat in a burst of one shape, as a program write does while stations
# and objects reload, eg. "Station bus command (1040) not processed on station 3 (84)".
# Pass the first of a burst, and pass one again hourly while the burst continues.
REPEAT_WINDOW = 60.0
REPEAT_REMINDER = 3600.0
NUMBER = re.compile(r"\d+")

type Shape = tuple[str, str, tuple[str, ...]]


def is_notable(log: SystemLogReceived) -> bool:
    """Return whether a system log is worth an event."""
    return log.type in NOTABLE_TYPES or bool(MODULE_LOG.match(log.message))


def is_noise(
    log: SystemLogReceived, seen: dict[Shape, tuple[float, float]], now: float
) -> bool:
    """Return whether a notable warning is compiler output or a repeat in a burst."""
    if log.type != SysLogType.WARNING:
        return False
    if log.message.startswith("[ALERT]") or MODULE_LOG.match(log.message):
        return False
    if log.source == COMPILER_SOURCE or SCRIPT_COMPILED.search(log.message):
        return True

    # The shape ignores numbers but keeps every VID the message names, so warnings about
    # different VIDs stay apart, while a burst over numbered stations or commands collapses
    vids = tuple(match.group(0) for match in OBJECT_VID.finditer(log.message))
    shape = (log.source, NUMBER.sub("N", log.message), vids)
    last_seen, last_passed = seen.get(shape, (None, None))
    if (
        last_seen is not None
        and last_passed is not None
        and now - last_seen < REPEAT_WINDOW
        and now - last_passed < REPEAT_REMINDER
    ):
        seen[shape] = (now, last_passed)
        return True

    seen[shape] = (now, now)
    return False


@callback
def async_setup_system_log(hass: HomeAssistant, entry: VantageConfigEntry) -> None:
    """Fire an event for each notable system log, given the enhanced log."""
    vantage = entry.runtime_data.client
    names = entry.runtime_data.object_names

    seen: dict[Shape, tuple[float, float]] = {}

    async def fire(log: SystemLogReceived) -> None:
        payload: dict[str, object] = {
            "host": vantage.host,
            "type": log_type_name(log.type),
            "source": log.source,
            "message": log.message,
        }
        if match := OBJECT_VID.search(log.message):
            vid = int(match.group(1) or match.group(2))
            # Still fire the log when its object can't be named
            try:
                name = await names.name(vid)
            except Exception:
                LOGGER.exception("Failed to name VID %d in a system log", vid)
                name = f"VID {vid}"
            payload |= {"object_id": vid, "object_name": name}

        hass.bus.async_fire(EVENT_SYSTEM_LOG, payload)

    # Fire in the order the controller logged, though some names need a lookup
    worker = OrderedWorker(hass, entry, fire, "vantage_system_log")

    def on_system_log(log: SystemLogReceived) -> None:
        if not is_notable(log):
            return

        # A module dropped off or rejoined the bus, refresh its connectivity now
        if MODULE_BUS_CHANGE.match(log.message):
            entry.async_create_background_task(
                hass,
                entry.runtime_data.module_status.async_request_refresh(),
                "vantage_module_status",
            )

        if is_noise(log, seen, time.monotonic()):
            return

        worker.put(log)

    entry.async_on_unload(
        vantage.event_stream.subscribe_system_log(on_system_log, peers=vantage.peers)
    )


def log_type_name(log_type: int | None) -> str:
    """Return the snake case name of a system log type, eg. "warning"."""
    if log_type is None:
        return "unknown"

    try:
        return SysLogType(log_type).name.lower()
    except ValueError:
        return f"unknown_{log_type}"
