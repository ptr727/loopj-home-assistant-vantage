"""The service that reads which modules respond on each enclosure bus."""

from typing import TYPE_CHECKING

# Type check against probatio, which Home Assistant 2026.9.0 and later load as voluptuous
# TODO: Import probatio directly once hacs.json requires Home Assistant 2026.9.0
if TYPE_CHECKING:
    import probatio as vol
else:
    import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util.json import JsonValueType

from .config_entry import VantageConfigEntry
from .const import SERVICE_GET_MODULE_STATUS
from .diagnostic_services import ReadError, async_register_response_service
from .module_status import read_module_status


async def _read_module_status(
    entry: VantageConfigEntry, call: ServiceCall, errors: list[ReadError]
) -> list[JsonValueType]:
    vantage = entry.runtime_data.client
    status = await read_module_status(vantage, entry.runtime_data.object_names)
    errors.extend(
        {"host": vantage.host, "enclosure": vid, "error": error}
        for vid, error in status.failed.items()
    )

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


def async_register_module_status_service(hass: HomeAssistant) -> None:
    """Register the response-only service that reads the module status."""
    async_register_response_service(
        hass,
        SERVICE_GET_MODULE_STATUS,
        vol.Schema({}),
        "modules",
        _read_module_status,
        "get_module_status_failed",
    )
