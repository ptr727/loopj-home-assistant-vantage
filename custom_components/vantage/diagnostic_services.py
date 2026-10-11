"""Services that read the controller's diagnostics, as InFusion Diagnostics shows them."""

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from aiovantage.config_client import DiagnosticInterface, SysLogType
from aiovantage.errors import ClientError, CommandError
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import JsonValueType

from .config_entry import VantageConfigEntry
from .const import (
    DOMAIN,
    SERVICE_GET_MODULE_STATUS,
    SERVICE_GET_SYSTEM_LOG,
    SERVICE_GET_TRIGGER_HISTORY,
)
from .module_status import read_module_status
from .triggers import describe_trigger

ATTR_LIMIT = "limit"

LOG_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_LIMIT, default=100): vol.All(int, vol.Range(min=1))}
)

# A reader returns what it read, and adds any master it couldn't read to the errors
type EntryReader = Callable[
    [VantageConfigEntry, ServiceCall, list[JsonValueType]],
    Awaitable[list[JsonValueType]],
]


def _masters(entry: VantageConfigEntry) -> list[int | None]:
    # None addresses the connected master, then each peer in a multi-master system
    return [None, *entry.runtime_data.client.peers]


def _master_error(
    entry: VantageConfigEntry, master: int | None, error: str
) -> JsonValueType:
    vantage = entry.runtime_data.client
    return {
        "host": vantage.host,
        "master": master or vantage.master_number,
        "error": error,
    }


def _read_failed(
    entry: VantageConfigEntry,
    masters: list[int | None],
    index: int,
    err: Exception,
    errors: list[JsonValueType],
) -> bool:
    """Record a master that couldn't be read, and return whether to read the next."""
    errors.append(_master_error(entry, masters[index], str(err) or type(err).__name__))

    # A peer that answered with an error leaves the connection in step. Peers are read
    # through the connected master, so after it fails, or after a timeout or a broken
    # connection, which can leave a late answer to be read as the next master's, stop.
    if masters[index] is not None and isinstance(err, CommandError):
        return True

    failed = masters[index] or entry.runtime_data.client.master_number
    errors.extend(
        _master_error(entry, master, f"Not read after master {failed} failed")
        for master in masters[index + 1 :]
    )
    return False


async def _trigger_history(
    entry: VantageConfigEntry, call: ServiceCall, errors: list[JsonValueType]
) -> list[JsonValueType]:
    vantage = entry.runtime_data.client

    entries: list[JsonValueType] = []
    masters = _masters(entry)
    for index, master in enumerate(masters):
        # Keep what the other masters returned when one can't be read
        try:
            log = await DiagnosticInterface.get_event_log(
                vantage.config_client, master=master
            )
        except (ClientError, TimeoutError) as err:
            if _read_failed(entry, masters, index, err, errors):
                continue
            break

        for item in log[-call.data[ATTR_LIMIT] :]:
            described = await describe_trigger(
                entry.runtime_data.object_names,
                item.source or 0,
                item.destination or 0,
                item.event,
                item.param1,
                item.param2,
            )
            # Times are in the controller's local time
            entries.append(
                {
                    "time": item.time_stamp,
                    "host": vantage.host,
                    "master": master or vantage.master_number,
                }
                | described
            )

    return entries


async def _system_log(
    entry: VantageConfigEntry, call: ServiceCall, errors: list[JsonValueType]
) -> list[JsonValueType]:
    vantage = entry.runtime_data.client

    entries: list[JsonValueType] = []
    masters = _masters(entry)
    for index, master in enumerate(masters):
        try:
            log = await DiagnosticInterface.get_sys_log(
                vantage.config_client, master=master
            )
        except (ClientError, TimeoutError) as err:
            if _read_failed(entry, masters, index, err, errors):
                continue
            break

        entries.extend(
            {
                # Month, day and time in the controller's local time, eg. "01-02T03:04:05"
                "time": item.time,
                "host": vantage.host,
                "master": master or vantage.master_number,
                "type": _sys_log_type_name(item.log_type, item.type),
                "source": item.src,
                "message": (item.msg or "").strip(),
            }
            for item in log[-call.data[ATTR_LIMIT] :]
        )

    return entries


async def _module_status(
    entry: VantageConfigEntry, call: ServiceCall, errors: list[JsonValueType]
) -> list[JsonValueType]:
    vantage = entry.runtime_data.client
    status = await read_module_status(entry)

    modules: list[JsonValueType] = []
    for module in vantage.modules:
        info = status.modules.get(module.vid)
        modules.append(
            {
                "host": vantage.host,
                "module_id": module.vid,
                "module_name": module.name,
                # None when the module's enclosure couldn't be read
                "online": status.is_online(module),
                "address": info.address if info else None,
                "model": info.model if info else None,
                "version": info.version if info else None,
                "status": [info.status.proc0, info.status.proc1]
                if info and info.status
                else None,
                "errors": info.errors if info else None,
            }
        )

    return modules


def _sys_log_type_name(log_type: SysLogType | None, raw: int | None) -> str:
    return log_type.name.lower() if log_type is not None else f"unknown_{raw}"


def async_register_diagnostic_services(hass: HomeAssistant) -> None:
    """Register the response-only services that read controller diagnostics."""

    def response_service(
        key: str, read: EntryReader, what: str
    ) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse]]:
        async def handler(call: ServiceCall) -> ServiceResponse:
            items: list[JsonValueType] = []
            errors: list[JsonValueType] = []
            for entry in hass.config_entries.async_entries(DOMAIN):
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                try:
                    items.extend(await read(entry, call, errors))
                except (ClientError, TimeoutError) as err:
                    errors.append(
                        {"host": entry.runtime_data.client.host, "error": str(err)}
                    )

            # Report what could be read, and only fail when nothing could
            if errors and not items:
                raise HomeAssistantError(f"Failed to read {what}: {errors}")

            return {key: items, "errors": errors}

        return handler

    for service, handler, schema in (
        (
            SERVICE_GET_TRIGGER_HISTORY,
            response_service("entries", _trigger_history, "trigger history"),
            LOG_SCHEMA,
        ),
        (
            SERVICE_GET_SYSTEM_LOG,
            response_service("entries", _system_log, "system log"),
            LOG_SCHEMA,
        ),
        (
            SERVICE_GET_MODULE_STATUS,
            response_service("modules", _module_status, "module status"),
            vol.Schema({}),
        ),
    ):
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(
                DOMAIN,
                service,
                handler,
                schema=schema,
                supports_response=SupportsResponse.ONLY,
            )
