"""Track which modules respond on each controller's enclosure bus."""

from collections import defaultdict
from datetime import timedelta

from aiovantage.config_client import DiagnosticInterface, ModuleInfo
from aiovantage.errors import ClientError
from aiovantage.objects import SystemObject

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_entry import VantageConfigEntry
from .const import LOGGER

# Polled as a fallback, "Module Offline/Online" system logs trigger a refresh at once
MODULE_STATUS_INTERVAL = timedelta(seconds=30)


class ModuleStatusCoordinator(DataUpdateCoordinator[dict[int, ModuleInfo]]):
    """Poll the modules that respond on the bus, keyed by module VID.

    A module missing from the bus has lost power, eg. its breaker tripped.
    """

    config_entry: VantageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: VantageConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name="Vantage module status",
            update_interval=MODULE_STATUS_INTERVAL,
        )

    async def _async_update_data(self) -> dict[int, ModuleInfo]:
        vantage = self.config_entry.runtime_data.client

        # Enclosures are numbered from 1 on each master, so count them per master
        enclosures: defaultdict[int, set[int]] = defaultdict(set)
        for module in vantage.modules:
            enclosures[module.master].add(_parent_vid(module))

        found: dict[int, ModuleInfo] = {}
        try:
            for master, parents in enclosures.items():
                address = None if master == vantage.master_number else master
                for enclosure in range(1, len(parents) + 1):
                    for info in await DiagnosticInterface.get_modules(
                        vantage.config_client, enclosure, master=address
                    ):
                        if info.vid is not None:
                            found[info.vid] = info
        except ClientError as err:
            raise UpdateFailed(f"Failed to read module status: {err}") from err

        return found


def _parent_vid(module: SystemObject) -> int:
    parent = getattr(module, "parent", None)
    return getattr(parent, "vid", 0)
