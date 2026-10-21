"""Track which modules respond on each controller's enclosure bus."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from aiovantage import Vantage
from aiovantage.config_client import DiagnosticInterface, ModuleInfo
from aiovantage.errors import ClientError
from aiovantage.objects import SystemObject

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER
from .names import ObjectNames

# Polled as a fallback, "Module Offline/Online" system logs trigger a refresh at once
MODULE_STATUS_INTERVAL = timedelta(seconds=30)

UNLOADED = "Not read, the integration was unloaded"


@dataclass
class ModuleStatus:
    """The modules responding on the bus, and the enclosures that could be read."""

    modules: dict[int, ModuleInfo] = field(default_factory=dict[int, ModuleInfo])
    """Responding modules, keyed by module VID."""

    enclosures: set[int] = field(default_factory=set[int])
    """VIDs of the enclosures whose bus was read."""

    failed: dict[int, str] = field(default_factory=dict[int, str])
    """Enclosures whose bus couldn't be read, keyed by VID, with the error."""

    def is_online(self, module: SystemObject) -> bool | None:
        """Return whether a module responds, or None if its bus couldn't be read."""
        if enclosure_vid(module) not in self.enclosures:
            return None

        return module.vid in self.modules


def enclosure_vid(module: SystemObject) -> int | None:
    """Return the VID of the enclosure a module is mounted in."""
    return getattr(getattr(module, "parent", None), "vid", None)


async def read_module_status(vantage: Vantage, names: ObjectNames) -> ModuleStatus:
    """Read the modules responding on the bus of each enclosure holding modules."""
    # Each enclosure is addressed by its position on its master
    enclosures: dict[int, int] = {}
    for module in vantage.modules:
        if (vid := enclosure_vid(module)) is not None:
            enclosures[vid] = module.master

    status = ModuleStatus()
    for vid, master in enclosures.items():
        number = (await names.info(vid)).position

        # The entry was unloaded mid-read, so don't reopen its closed client. Checked
        # after the lookup, which unload can land in or cancel, and right before the read.
        if names.closed:
            status.failed[vid] = UNLOADED
            continue

        if number is None:
            LOGGER.debug("Enclosure %d has no position, skipping", vid)
            status.failed[vid] = "Enclosure position could not be read"
            continue

        # Read each enclosure separately, so one failure doesn't hide the others
        address = None if master == vantage.master_number else master
        try:
            found = await DiagnosticInterface.get_modules(
                vantage.config_client, number, master=address
            )
        except Exception as err:
            # aiovantage can also raise decode or XML errors that aren't a ClientError
            LOGGER.debug("Failed to read modules in enclosure %d: %s", vid, err)
            status.failed[vid] = (
                UNLOADED if names.closed else str(err) or type(err).__name__
            )
            continue

        status.enclosures.add(vid)
        status.modules |= {info.vid: info for info in found if info.vid is not None}

    return status


class ModuleStatusCoordinator(DataUpdateCoordinator[ModuleStatus]):
    """Poll the modules that respond on the bus.

    A module missing from the bus has lost power, eg. its breaker tripped.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        vantage: Vantage,
        names: ObjectNames,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name="Vantage module status",
            update_interval=MODULE_STATUS_INTERVAL,
        )
        self._vantage = vantage
        self._names = names

        # No bus read yet, so every module's connectivity is unknown
        self.data = ModuleStatus()

    async def _async_update_data(self) -> ModuleStatus:
        try:
            status = await read_module_status(self._vantage, self._names)
        except ClientError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="module_status_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        # Fail the update, as before, when no enclosure could be read at all, but not when
        # the entry was unloaded mid-read, which is no failure worth an error
        if status.failed and not status.enclosures and not self._names.closed:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_enclosure_read",
                translation_placeholders={"errors": str(status.failed)},
            )

        return status
