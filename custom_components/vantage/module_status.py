"""Track which modules respond on each controller's enclosure bus."""

from dataclasses import dataclass, field
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


@dataclass
class ModuleStatus:
    """The modules responding on the bus, and the enclosures that could be read."""

    modules: dict[int, ModuleInfo] = field(default_factory=dict[int, ModuleInfo])
    """Responding modules, keyed by module VID."""

    enclosures: set[int] = field(default_factory=set[int])
    """VIDs of the enclosures whose bus was read."""

    def is_online(self, module: SystemObject) -> bool | None:
        """Return whether a module responds, or None if its bus couldn't be read."""
        if enclosure_vid(module) not in self.enclosures:
            return None

        return module.vid in self.modules


def enclosure_vid(module: SystemObject) -> int | None:
    """Return the VID of the enclosure a module is mounted in."""
    return getattr(getattr(module, "parent", None), "vid", None)


async def read_module_status(entry: VantageConfigEntry) -> ModuleStatus:
    """Read the modules responding on the bus of each enclosure holding modules."""
    vantage = entry.runtime_data.client
    names = entry.runtime_data.object_names

    # Each enclosure is addressed by its position on its master
    enclosures: dict[int, int] = {}
    for module in vantage.modules:
        if (vid := enclosure_vid(module)) is not None:
            enclosures[vid] = module.master

    status = ModuleStatus()
    for vid, master in enclosures.items():
        number = (await names.info(vid)).position
        if number is None:
            LOGGER.debug("Enclosure %d has no position, skipping", vid)
            continue

        # Read each enclosure separately, so one failure doesn't hide the others
        address = None if master == vantage.master_number else master
        try:
            found = await DiagnosticInterface.get_modules(
                vantage.config_client, number, master=address
            )
        except ClientError as err:
            LOGGER.debug("Failed to read modules in enclosure %d: %s", vid, err)
            continue

        status.enclosures.add(vid)
        status.modules |= {info.vid: info for info in found if info.vid is not None}

    if enclosures and not status.enclosures:
        raise ClientError("No enclosure could be read")

    return status


class ModuleStatusCoordinator(DataUpdateCoordinator[ModuleStatus]):
    """Poll the modules that respond on the bus.

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

    async def _async_update_data(self) -> ModuleStatus:
        try:
            return await read_module_status(self.config_entry)
        except ClientError as err:
            raise UpdateFailed(f"Failed to read module status: {err}") from err
