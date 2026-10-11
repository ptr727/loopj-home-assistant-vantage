"""Support for Vantage binary sensor entities."""

from typing import Any, override

from aiovantage.controllers import Controller
from aiovantage.objects import DryContact, SystemObject

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .config_entry import VantageConfigEntry
from .entity import VantageEntity, add_entities_from_controller
from .module_status import ModuleStatus, ModuleStatusCoordinator


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
    coordinator = ModuleStatusCoordinator(hass, entry)
    await coordinator.async_refresh()
    entry.runtime_data.module_status = coordinator
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


class VantageModuleConnectivityEntity(VantageEntity[SystemObject], BinarySensorEntity):
    """Binary sensor reporting whether a module responds on its enclosure bus.

    A module drops off the bus when it loses power, eg. when its breaker trips.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def coordinator(self) -> ModuleStatusCoordinator | None:
        """Return the module status coordinator."""
        return self.entry.runtime_data.module_status

    @property
    @override
    def unique_id(self) -> str:
        return f"{self.obj.vid}:connectivity"

    @property
    @override
    def name(self) -> str | None:
        # The module's device is named after the module, so this reads "<module> Connectivity"
        return "Connectivity"

    @property
    @override
    def available(self) -> bool:
        coordinator = self.coordinator
        return (
            super().available
            and coordinator is not None
            and coordinator.last_update_success
        )

    @property
    @override
    def is_on(self) -> bool | None:
        if (status := self._module_status()) is None:
            return None

        # None when the module's enclosure couldn't be read
        return status.is_online(self.obj)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self._module_status()
        if status is None or (info := status.modules.get(self.obj.vid)) is None:
            return None

        # The error count rises each time the module loses and regains the bus
        return {"address": info.address, "errors": info.errors}

    def _module_status(self) -> ModuleStatus | None:
        # Unset until the first successful refresh, despite the coordinator's typing
        if (coordinator := self.coordinator) is None:
            return None

        status: ModuleStatus | None = coordinator.data
        return status

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if coordinator := self.coordinator:
            self.async_on_remove(
                coordinator.async_add_listener(self.async_write_ha_state)
            )
