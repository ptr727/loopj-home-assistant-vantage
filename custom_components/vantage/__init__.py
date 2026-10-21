"""The Vantage InFusion Controller integration."""

import asyncio

from aiovantage import Vantage
from aiovantage.errors import (
    ClientConnectionError,
    LoginFailedError,
    LoginRequiredError,
)
from aiovantage.events import Connected, Disconnected, ObjectUpdated, Reconnected
from aiovantage.objects import Master

from homeassistant.config_entries import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SSL,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.ssl import get_default_no_verify_context

from .config_entry import VantageConfigEntry, VantageData
from .const import DOMAIN, LOGGER
from .device import async_cleanup_devices, async_setup_devices
from .entity import async_cleanup_entities
from .events import async_setup_events
from .migrate import async_migrate_data
from .module_status import ModuleStatusCoordinator
from .names import ObjectNames
from .services import async_register_services
from .system_log import async_setup_system_log
from .triggers import async_setup_triggers

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

# How long to wait after receiving a system programming event before refreshing
SYSTEM_PROGRAMMING_DELAY = 30

# The integration is only set up from config entries, never from YAML
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # type: ignore


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Vantage integration."""
    # Register services (start task, stop task, etc.)
    async_register_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: VantageConfigEntry) -> bool:
    """Set up Vantage integration from a config entry."""
    # Create a Vantage client
    vantage = Vantage(
        entry.data[CONF_HOST],
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
        ssl=entry.data.get(CONF_SSL, True),
        ssl_context_factory=get_default_no_verify_context,
    )

    # Store the client in the config entry's runtime data, with the name resolver for
    # triggers and the coordinator polling which modules respond on the bus
    names = ObjectNames(vantage)
    entry.runtime_data = VantageData(
        client=vantage,
        entry_id=entry.entry_id,
        object_names=names,
        module_status=ModuleStatusCoordinator(hass, entry, vantage, names),
    )

    try:
        # Initialize and fetch all objects
        await vantage.initialize()

        # Add Vantage devices (controllers, modules, stations) to the device registry
        await async_setup_devices(hass, entry)

        # Generate events naming the object that started each task, and for notable
        # system logs, eg. a module losing power. Both are only streamed through the
        # enhanced log, whose support is checked before the platforms are set up, so a
        # connection failure here can retry setup from scratch.
        if (await vantage.event_stream.start()).supports_enhanced_log:
            async_setup_triggers(hass, entry)
            async_setup_system_log(hass, entry)
        else:
            LOGGER.info(
                "Controller firmware has no enhanced log, so no trigger or system log "
                "events will fire"
            )

        # Read which modules respond on the bus, for the module connectivity sensors
        await entry.runtime_data.module_status.async_refresh()

        # Set up each platform (lights, covers, etc.)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Generate events for button presses, etc.
        async_setup_events(hass, entry)

        # Clean up any orphaned devices and entities
        async_cleanup_devices(hass, entry)
        async_cleanup_entities(hass, entry)

        # Run any migrations
        await async_migrate_data(hass, entry)

        # Subscribe to system programming events
        def on_master_updated(event: ObjectUpdated[Master]) -> None:
            # Return early if the m_time attribute did not change
            if "m_time" not in event.attrs_changed:
                return

            async def refresh_controllers() -> None:
                # The m_time attribute changes at the start of system programming.
                # Unfortunately, the Vantage controller does not send an event when
                # programming ends, so we must wait for a short time before refreshing
                # controllers to avoid fetching incomplete data.
                await asyncio.sleep(SYSTEM_PROGRAMMING_DELAY)
                await vantage.initialize()

                # Names of objects the integration doesn't load may have changed too
                entry.runtime_data.object_names.clear()

            # Tied to the entry, so an unload during the wait cancels it
            entry.async_create_background_task(
                hass, refresh_controllers(), "vantage_refresh_controllers"
            )

        entry.async_on_unload(
            vantage.masters.subscribe(ObjectUpdated, on_master_updated)
        )

        # Mark every entity unavailable while the controller connection is down
        def on_connection_changed(
            event: Connected | Reconnected | Disconnected,
        ) -> None:
            entry.runtime_data.connected = not isinstance(event, Disconnected)
            async_dispatcher_send(hass, entry.runtime_data.signal_connection_changed)

        for event_type in (Connected, Reconnected, Disconnected):
            entry.async_on_unload(
                vantage.event_stream.subscribe(event_type, on_connection_changed)
            )

    except (LoginFailedError, LoginRequiredError) as err:
        # Handle expired or invalid credentials. This will prompt the user to
        # reconfigure the integration.
        raise ConfigEntryAuthFailed from err

    except ClientConnectionError as err:
        # Handle connection errors. Home Assistant will automatically take care of
        # retrying set up later.
        raise ConfigEntryNotReady from err

    return True


async def async_unload_entry(hass: HomeAssistant, entry: VantageConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload all platforms first, so nothing polls the client once it is closed
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Close the Vantage client connection, once no lookup can reopen it
    entry.runtime_data.object_names.cancel()
    entry.runtime_data.client.close()

    return unloaded
