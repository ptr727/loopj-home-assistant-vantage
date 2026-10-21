"""Tests for the Vantage InFusion binary sensor platform."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from aiovantage import Vantage
from aiovantage.config_client import ModuleInfo
from aiovantage.errors import ClientConnectionError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.vantage.const import UNLOADED
from custom_components.vantage.module_status import ModuleStatus, read_module_status
from custom_components.vantage.names import ObjectNames
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import ENCLOSURE_VID


def client_and_names(entry: MockConfigEntry) -> tuple[Vantage, ObjectNames]:
    """Return the client and name resolver the module status is read with."""
    return entry.runtime_data.client, entry.runtime_data.object_names


@pytest.mark.parametrize("init_integration", [Platform.BINARY_SENSOR], indirect=True)
@pytest.mark.usefixtures("entity_registry_enabled_by_default", "init_integration")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the binary sensor entities."""
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def async_refresh_module_status(
    hass: HomeAssistant, entry: MockConfigEntry, status: ModuleStatus
) -> None:
    """Refresh the module status coordinator, reading the given status."""
    coordinator = entry.runtime_data.module_status
    with patch(
        "custom_components.vantage.module_status.read_module_status",
        return_value=status,
    ):
        await coordinator.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("init_integration")
async def test_module_connectivity_disabled_by_default(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Test that the module connectivity sensors start disabled."""
    assert (entry := entity_registry.async_get("binary_sensor.module_1_connectivity"))
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("binary_sensor.module_1_connectivity") is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "init_integration")
async def test_module_connectivity(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test a module dropping off its enclosure's bus and rejoining it."""
    entity_id = "binary_sensor.module_1_connectivity"
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON
    assert state.attributes["address"] == 1
    assert state.attributes["errors"] == 0

    # Off the bus, keeping the address and error count it was last seen with
    await async_refresh_module_status(
        hass, mock_config_entry, ModuleStatus(enclosures={1})
    )
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_OFF
    assert state.attributes["address"] == 1
    assert state.attributes["errors"] == 0

    # Back on the bus, with one more error
    await async_refresh_module_status(
        hass,
        mock_config_entry,
        ModuleStatus(
            modules={3: ModuleInfo(address=1, vid=3, errors=1)}, enclosures={1}
        ),
    )
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON
    assert state.attributes["errors"] == 1

    # Unknown while its enclosure can't be read, though another enclosure was
    await async_refresh_module_status(
        hass,
        mock_config_entry,
        ModuleStatus(enclosures={ENCLOSURE_VID}, failed={1: "ClientConnectionError"}),
    )
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "init_integration")
async def test_module_connectivity_unavailable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that the sensors are unavailable while no enclosure can be read."""
    await async_refresh_module_status(
        hass, mock_config_entry, ModuleStatus(failed={1: "ClientConnectionError"})
    )

    assert (state := hass.states.get("binary_sensor.module_1_connectivity"))
    assert state.state == STATE_UNAVAILABLE
    assert (
        "No enclosure could be read: enclosure 1: ClientConnectionError" in caplog.text
    )


@pytest.mark.usefixtures("init_integration")
async def test_module_status_refresh_at_unload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a refresh the unload interrupts doesn't fail or log an error."""
    coordinator = mock_config_entry.runtime_data.module_status
    mock_config_entry.runtime_data.object_names.cancel()

    await async_refresh_module_status(
        hass, mock_config_entry, ModuleStatus(failed={1: UNLOADED})
    )

    assert coordinator.last_update_success
    assert "No enclosure could be read" not in caplog.text


@pytest.mark.usefixtures("init_integration")
async def test_read_module_status_unload_after_lookup(
    mock_config_entry: MockConfigEntry, mock_enclosure: AsyncMock
) -> None:
    """Test that an unload landing in an enclosure's lookup stops the read."""
    names = mock_config_entry.runtime_data.object_names
    enclosure = mock_enclosure.return_value

    # The lookup completes, but the entry unloads while it was in flight
    async def unload_during_lookup(*args: Any) -> list[Any]:
        names.cancel()
        return enclosure

    mock_enclosure.side_effect = unload_during_lookup
    with patch(
        "custom_components.vantage.module_status.DiagnosticInterface.get_modules"
    ) as get_modules:
        status = await read_module_status(*client_and_names(mock_config_entry))

    get_modules.assert_not_awaited()
    assert status.failed == {ENCLOSURE_VID: UNLOADED}


@pytest.mark.usefixtures("init_integration")
async def test_read_module_status_lookup_cancelled(
    mock_config_entry: MockConfigEntry, mock_enclosure: AsyncMock
) -> None:
    """Test that an enclosure whose lookup the unload cancelled is reported unloaded."""
    names = mock_config_entry.runtime_data.object_names
    looking_up = asyncio.Event()

    async def lookup_forever(*args: Any) -> list[Any]:
        looking_up.set()
        await asyncio.Event().wait()
        return []

    mock_enclosure.side_effect = lookup_forever
    with patch(
        "custom_components.vantage.module_status.DiagnosticInterface.get_modules"
    ) as get_modules:
        read = asyncio.ensure_future(
            read_module_status(*client_and_names(mock_config_entry))
        )
        await looking_up.wait()
        names.cancel()
        status = await read

    get_modules.assert_not_awaited()
    assert status.failed == {ENCLOSURE_VID: UNLOADED}


@pytest.mark.usefixtures("init_integration", "mock_enclosure")
async def test_read_module_status_read_interrupted(
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that an enclosure whose read the unload interrupted is reported unloaded."""
    names = mock_config_entry.runtime_data.object_names

    async def unload_during_read(*args: Any, **kwargs: Any) -> list[Any]:
        names.cancel()
        raise ClientConnectionError

    with patch(
        "custom_components.vantage.module_status.DiagnosticInterface.get_modules",
        side_effect=unload_during_read,
    ):
        status = await read_module_status(*client_and_names(mock_config_entry))

    assert status.failed == {ENCLOSURE_VID: UNLOADED}
