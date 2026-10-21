"""Support for Vantage binary sensor entities."""

from typing import Any, override

from aiovantage.config_client import ModuleInfo
from aiovantage.controllers import Controller
from aiovantage.objects import DryContact, SystemObject

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .config_entry import VantageConfigEntry
from .entity import VantageEntity, add_entities_from_controller
from .module_status import ModuleStatusCoordinator

# Let aiovantage queue requests to the controller
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VantageConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vantage binary sensor entities from a config entry."""
    vantage = entry.runtime_data.client

    # Add every dry contact as a binary sensor entity
    add_entities_from_controller(
        entry, async_add_entities, VantageBinarySensorEntity, vantage.dry_contacts
    )

    # Add a connectivity binary sensor for each module, off when it drops off the bus
    add_entities_from_controller(
        entry, async_add_entities, VantageModuleConnectivityEntity, vantage.modules
    )


class VantageBinarySensorEntity(VantageEntity[DryContact], BinarySensorEntity):
    """Binary sensor entity provided by a Vantage DryContact object."""

    def __init__(
        self,
        entry: VantageConfigEntry,
        controller: Controller[DryContact],
        obj: DryContact,
    ):
        """Initialize a Vantage binary sensor entity."""
        super().__init__(entry, controller, obj)

        # If this is a thermostat contact, attach it to the thermostat device
        if parent := self.client.thermostats.get(self.obj.parent.vid):
            self.parent_obj = parent

    @property
    @override
    def is_on(self) -> bool | None:
        return self.obj.is_down


class VantageModuleConnectivityEntity(
    CoordinatorEntity[ModuleStatusCoordinator],
    VantageEntity[SystemObject],
    BinarySensorEntity,
):
    """Binary sensor reporting whether a module responds on its enclosure bus.

    A module drops off the bus when it loses power, eg. when its breaker trips.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    # Kept while the module is off the bus, when the controller no longer lists it
    _last_info: ModuleInfo | None = None

    def __init__(
        self,
        entry: VantageConfigEntry,
        controller: Controller[SystemObject],
        obj: SystemObject,
    ):
        """Initialize a Vantage module connectivity entity."""
        # Neither base initializer calls the next, so each is called in turn
        super().__init__(entry.runtime_data.module_status)
        VantageEntity.__init__(self, entry, controller, obj)  # pyright: ignore[reportUnknownMemberType]

    @property
    @override
    def unique_id(self) -> str:
        return f"{self.obj.vid}:connectivity"

    @property
    @override
    def name(self) -> str | None:
        # Named by its translated device class, after the module's device, so this reads
        # "<module> Connectivity" in English
        return self._device_class_name

    @property
    @override
    def available(self) -> bool:
        # The coordinator's last read must have succeeded, and, as for every Vantage
        # entity, the controller connection must be up
        return super().available and self.entry.runtime_data.connected

    @property
    @override
    def is_on(self) -> bool | None:
        # None when the module's enclosure couldn't be read
        return self.coordinator.data.is_online(self.obj)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (info := self._last_info) is None:
            return None

        # The error count rises each time the module loses and regains the bus
        return {"address": info.address, "errors": info.errors}

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._record_info()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._record_info()
        super()._handle_coordinator_update()

    def _record_info(self) -> None:
        if (info := self.coordinator.data.modules.get(self.obj.vid)) is not None:
            self._last_info = info
