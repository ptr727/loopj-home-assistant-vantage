"""Track which modules respond on each controller's enclosure bus."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
import time
from typing import Any, override

from aiovantage import Vantage
from aiovantage.config_client import DiagnosticInterface, ModuleInfo
from aiovantage.errors import ClientError
from aiovantage.objects import SystemObject

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER, UNLOADED
from .names import ObjectNames

# Polled as a fallback while a sensor listens, "Module Offline/Online" system logs
# trigger a refresh at once
MODULE_STATUS_INTERVAL = timedelta(seconds=60)

# A program write re-announces the modules one at a time, each with a "Module Online"
# log. The read the first log triggers misses the modules not yet announced, and the
# coordinator's default 10 s cooldown then held off the read the next logs asked for, so
# those modules showed off the bus for 10 s. Refresh soon after a log instead.
REFRESH_COOLDOWN = 1.0

# A module that answered the last read and is missing from this one is held online and
# read again after this delay, and is reported off the bus only if it's still missing.
# A module that really lost power stays off the bus, and a "Module Offline" log reports
# it at once anyway.
MISSING_CONFIRM_DELAY = 5.0


@dataclass
class ModuleStatus:
    """The modules responding on the bus, and the enclosures that could be read."""

    modules: dict[int, ModuleInfo] = field(default_factory=dict[int, ModuleInfo])
    """Responding modules, keyed by module VID."""

    enclosures: set[int] = field(default_factory=set[int])
    """VIDs of the enclosures whose bus was read."""

    failed: dict[int, str] = field(default_factory=dict[int, str])
    """Enclosures whose bus couldn't be read, keyed by VID, with the error."""

    overrides: dict[int, bool] = field(default_factory=dict[int, bool])
    """Modules whose state isn't the read's, keyed by VID, with whether they're online.

    A "Module Online/Offline" log newer than the read sets it, and so does a module held
    online while a confirming read is pending.
    """

    def is_online(self, module: SystemObject) -> bool | None:
        """Return whether a module responds, or None if its bus couldn't be read."""
        if (online := self.overrides.get(module.vid)) is not None:
            return online

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

    A module missing from the bus has lost power, eg. its breaker tripped. The bus is
    only read while a connectivity sensor listens, and they're disabled by default.
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
            request_refresh_debouncer=Debouncer(
                hass, LOGGER, cooldown=REFRESH_COOLDOWN, immediate=True
            ),
        )
        self._entry = entry
        self._vantage = vantage
        self._names = names

        # No bus read yet, so every module's connectivity is unknown
        self.data = ModuleStatus()

        # The last "Module Online/Offline" log of each module, keyed by VID, with whether
        # it came online and the monotonic time it arrived
        self._logs: dict[int, tuple[bool, float]] = {}

        # Modules held online while a confirming read is pending, and that read's timer
        self._held: set[int] = set()
        self._cancel_confirm: CALLBACK_TYPE | None = None

    @callback
    @override
    def async_add_listener(
        self, update_callback: CALLBACK_TYPE, context: Any = None
    ) -> Callable[[], None]:
        first = not self._listeners
        remove = super().async_add_listener(update_callback, context)

        # The first sensor enabled reads the bus at once, rather than at the first poll,
        # without the debouncer's cooldown, so a module log soon after still refreshes
        if first:
            self._entry.async_create_background_task(
                self.hass, self.async_refresh(), "vantage_module_status"
            )

        return remove

    @callback
    def async_refresh_if_listened(self) -> None:
        """Refresh at once, but only while a sensor listens."""
        if self._listeners:
            self._entry.async_create_background_task(
                self.hass, self.async_request_refresh(), "vantage_module_status"
            )

    @callback
    def async_module_logged(self, vid: int | None, online: bool) -> None:
        """Apply a "Module Online/Offline" log at once, then read the bus."""
        if not self._listeners:
            return

        if vid is not None:
            self._logs[vid] = (online, time.monotonic())
            self._held.discard(vid)
            self.data = replace(
                self.data, overrides=self.data.overrides | {vid: online}
            )
            self.async_update_listeners()

        self.async_refresh_if_listened()

    @override
    async def async_shutdown(self) -> None:
        self._cancel_confirm_read()
        await super().async_shutdown()

    def _cancel_confirm_read(self) -> None:
        if self._cancel_confirm is not None:
            self._cancel_confirm()
            self._cancel_confirm = None

    @callback
    def _confirm_read(self, _now: Any) -> None:
        self._cancel_confirm = None
        self.async_refresh_if_listened()

    def _reconcile(self, status: ModuleStatus, started: float) -> ModuleStatus:
        """Apply the logs newer than a read, and hold modules it newly misses."""
        # A log that arrived after the read started is newer than what it found
        status.overrides = {
            vid: online for vid, (online, at) in self._logs.items() if at >= started
        }

        previous = self.data
        held: set[int] = set()
        for module in self._vantage.modules:
            vid = module.vid
            if (
                vid in status.modules
                or vid in status.overrides
                or enclosure_vid(module) not in status.enclosures
            ):
                continue

            # Missing from a read when it was online, and not yet confirmed missing
            if previous.is_online(module) and vid not in self._held:
                status.overrides[vid] = True
                held.add(vid)

        self._held = held
        self._cancel_confirm_read()
        if held:
            LOGGER.debug("Modules %s missing from the bus, reading it again", held)
            self._cancel_confirm = async_call_later(
                self.hass, MISSING_CONFIRM_DELAY, self._confirm_read
            )

        return status

    async def _async_update_data(self) -> ModuleStatus:
        started = time.monotonic()
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
                translation_placeholders={
                    "errors": "; ".join(
                        f"enclosure {vid}: {error}"
                        for vid, error in status.failed.items()
                    )
                },
            )

        return self._reconcile(status, started)
