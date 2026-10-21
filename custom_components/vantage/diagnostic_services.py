"""Services that read the controller's diagnostics, as InFusion Diagnostics shows them."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiovantage import Vantage
from aiovantage.config_client import DiagnosticInterface
from aiovantage.errors import ClientResponseError

# Type check against probatio, which Home Assistant 2026.9.0 and later load as voluptuous
# TODO: Import probatio directly once hacs.json requires Home Assistant 2026.9.0
if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.util.json import JsonValueType

from .config_entry import VantageConfigEntry
from .const import (
    DOMAIN,
    SERVICE_GET_SYSTEM_LOG,
    SERVICE_GET_TRIGGER_HISTORY,
    UNLOADED,
)
from .system_log import log_type_name
from .triggers import describe_trigger

ATTR_LIMIT = "limit"

# The most entries returned per master, as services.yaml offers
MAX_LIMIT = 5000

LOG_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_LIMIT, default=100): vol.All(
            cv.positive_int, vol.Range(min=1, max=MAX_LIMIT)
        )
    }
)

# What couldn't be read, eg. {"host": "192.0.2.10", "master": 2, "error": "Timeout"}
type ReadError = dict[str, JsonValueType]

# A reader returns what it read, and adds any master it couldn't read to the errors
type EntryReader = Callable[
    [VantageConfigEntry, ServiceCall, list[ReadError]],
    Awaitable[list[JsonValueType]],
]


def _masters(vantage: Vantage) -> list[int | None]:
    # None addresses the connected master, then each peer in a multi-master system
    return [None, *vantage.peers]


def _master_error(vantage: Vantage, master: int | None, error: str) -> ReadError:
    return {
        "host": vantage.host,
        "master": master or vantage.master_number,
        "error": error,
    }


def _not_read(
    vantage: Vantage, masters: list[int | None], reason: str
) -> list[ReadError]:
    return [_master_error(vantage, master, reason) for master in masters]


def _read_failed(
    vantage: Vantage,
    masters: list[int | None],
    index: int,
    err: Exception,
    errors: list[ReadError],
) -> bool:
    """Record a master that couldn't be read, and return whether to read the next."""
    errors.append(_master_error(vantage, masters[index], _error_text(err)))

    # A peer's error answer is read in full before it is reported, so the shared
    # connection stays in step and the next peer can be read. Any other failure stops
    # the reading: the connected master's own failure, a connection error, timeout or
    # failed login, or an error aiovantage doesn't wrap, eg. a decode or XML error.
    if masters[index] is not None and isinstance(err, ClientResponseError):
        return True

    # Peers are read through the connected master's connection, which these failures
    # can leave unusable or out of step
    failed = masters[index] or vantage.master_number
    errors.extend(
        _not_read(
            vantage, masters[index + 1 :], f"Not read after master {failed} failed"
        )
    )
    return False


def format_errors(errors: list[ReadError]) -> str:
    """Return errors as text, eg. "master 2: Timeout; master 3: Not read"."""
    several_hosts = len({str(error["host"]) for error in errors}) > 1
    parts: list[str] = []
    for error in errors:
        if "master" in error:
            where = f"master {error['master']}"
        else:
            where = str(error["host"])

        # Name the controller too, when more than one couldn't be read
        if several_hosts and where != error["host"]:
            where = f"{error['host']} {where}"
        parts.append(f"{where}: {error['error']}")

    return "; ".join(parts)


def _error_text(err: Exception) -> str:
    # A read timeout or a dropped connection carries no message
    return str(err) or type(err).__name__


async def _trigger_history(
    entry: VantageConfigEntry, call: ServiceCall, errors: list[ReadError]
) -> list[JsonValueType]:
    # Captured once, since HA deletes runtime_data after an unload
    vantage = entry.runtime_data.client
    names = entry.runtime_data.object_names

    entries: list[JsonValueType] = []
    masters = _masters(vantage)
    for index, master in enumerate(masters):
        # The entry was unloaded mid-read, so don't reopen its closed client
        if names.closed:
            errors.extend(_not_read(vantage, masters[index:], UNLOADED))
            break

        # Keep what the other masters returned when one can't be read
        try:
            log = await DiagnosticInterface.get_event_log(
                vantage.config_client, master=master
            )
        except Exception as err:
            if _read_failed(vantage, masters, index, err, errors):
                continue
            break

        for item in log[-call.data[ATTR_LIMIT] :]:
            described = await describe_trigger(
                names,
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
    entry: VantageConfigEntry, call: ServiceCall, errors: list[ReadError]
) -> list[JsonValueType]:
    # Captured once, since HA deletes runtime_data after an unload
    vantage = entry.runtime_data.client
    names = entry.runtime_data.object_names

    entries: list[JsonValueType] = []
    masters = _masters(vantage)
    for index, master in enumerate(masters):
        # The entry was unloaded mid-read, so don't reopen its closed client
        if names.closed:
            errors.extend(_not_read(vantage, masters[index:], UNLOADED))
            break

        try:
            log = await DiagnosticInterface.get_sys_log(
                vantage.config_client, master=master
            )
        except Exception as err:
            if _read_failed(vantage, masters, index, err, errors):
                continue
            break

        entries.extend(
            {
                # Month, day and time in the controller's local time, eg. "01-02T03:04:05"
                "time": item.time,
                "host": vantage.host,
                "master": master or vantage.master_number,
                "type": log_type_name(item.type),
                "source": item.src,
                "message": (item.msg or "").strip(),
            }
            for item in log[-call.data[ATTR_LIMIT] :]
        )

    return entries


def async_register_response_service(
    hass: HomeAssistant,
    service: str,
    schema: vol.Schema,
    key: str,
    read: EntryReader,
    failed_key: str,
) -> None:
    """Register a response-only service, returning what each loaded entry read."""

    async def handler(call: ServiceCall) -> ServiceResponse:
        entries: list[VantageConfigEntry] = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="not_loaded"
            )

        items: list[JsonValueType] = []
        errors: list[ReadError] = []
        for entry in entries:
            host = entry.runtime_data.client.host
            try:
                items.extend(await read(entry, call, errors))
            except Exception as err:
                errors.append({"host": host, "error": _error_text(err)})

        # Report what could be read, and only fail when nothing could
        if errors and not items:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=failed_key,
                translation_placeholders={"errors": format_errors(errors)},
            )

        return {key: items, "errors": list[JsonValueType](errors)}

    hass.services.async_register(  # type: ignore
        DOMAIN, service, handler, schema=schema, supports_response=SupportsResponse.ONLY
    )


def async_register_diagnostic_services(hass: HomeAssistant) -> None:
    """Register the response-only services that read the controller's logs."""
    async_register_response_service(
        hass,
        SERVICE_GET_TRIGGER_HISTORY,
        LOG_SCHEMA,
        "entries",
        _trigger_history,
        "get_trigger_history_failed",
    )
    async_register_response_service(
        hass,
        SERVICE_GET_SYSTEM_LOG,
        LOG_SCHEMA,
        "entries",
        _system_log,
        "get_system_log_failed",
    )
