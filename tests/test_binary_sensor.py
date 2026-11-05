"""Tests for the Vantage InFusion binary sensor platform."""

import asyncio
from collections.abc import Generator
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from aiovantage import Vantage
from aiovantage.config_client import ModuleInfo, SysLogType
from aiovantage.errors import ClientConnectionError
from aiovantage.events import SystemLogReceived
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.vantage.const import DOMAIN, SERVICE_GET_MODULE_STATUS, UNLOADED
from custom_components.vantage.module_status import ModuleStatus, read_module_status
from custom_components.vantage.names import ObjectNames
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import setup_integration
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


@pytest.fixture
def mock_get_modules(
    mock_vantage: Vantage, mock_enclosure: AsyncMock
) -> Generator[AsyncMock]:
    """Read the bus for real, where Module 1 answers, in place of the setup's status."""
    with (
        patch(
            "custom_components.vantage.module_status.read_module_status",
            side_effect=read_module_status,
        ),
        patch(
            "custom_components.vantage.module_status.DiagnosticInterface.get_modules",
            return_value=[ModuleInfo(address=1, vid=3, errors=0)],
        ) as get_modules,
    ):
        yield get_modules


async def async_log_module_offline(hass: HomeAssistant, vantage: Vantage) -> None:
    """Pass the integration a system log of Module 1 dropping off the bus."""
    subscribe = cast(MagicMock, vantage.event_stream.subscribe_system_log)
    subscribe.call_args.args[0](
        SystemLogReceived(SysLogType.INFORMATION, "Station", "Module Offline: 1.1 [3]")
    )
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_module_status_unread_while_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vantage: Vantage,
    mock_get_modules: AsyncMock,
) -> None:
    """Test that no bus is read while every connectivity sensor is disabled."""
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_get_modules.assert_not_awaited()

    # Nothing listens for a module going offline either
    await async_log_module_offline(hass, mock_vantage)
    mock_get_modules.assert_not_awaited()

    # The action still reads the bus when asked
    await hass.services.async_call(
        DOMAIN, SERVICE_GET_MODULE_STATUS, blocking=True, return_response=True
    )
    mock_get_modules.assert_awaited_once()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_module_status_read_while_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vantage: Vantage,
    mock_get_modules: AsyncMock,
) -> None:
    """Test that an enabled sensor reads the bus at setup and when a module drops off."""
    entity_id = "binary_sensor.module_1_connectivity"
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_get_modules.assert_awaited_once()
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON

    mock_get_modules.return_value = []
    await async_log_module_offline(hass, mock_vantage)
    assert mock_get_modules.await_count == 2
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_OFF


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
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test a module dropping off its enclosure's bus and rejoining it."""
    entity_id = "binary_sensor.module_1_connectivity"
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON
    assert state.attributes["address"] == 1
    assert state.attributes["errors"] == 0

    # Missing from one read, so held online until a second read confirms it
    await async_refresh_module_status(
        hass, mock_config_entry, ModuleStatus(enclosures={1})
    )
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON

    # Off the bus, keeping the address and error count it was last seen with
    freezer.tick(timedelta(seconds=6))
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


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_module_missing_confirmed_by_second_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_get_modules: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test that a module missing from a read is read again before it goes off."""
    entity_id = "binary_sensor.module_1_connectivity"
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_get_modules.await_count == 1

    # The poll misses it, so it's held online and the bus is read again shortly
    mock_get_modules.return_value = []
    await mock_config_entry.runtime_data.module_status.async_refresh()
    await hass.async_block_till_done()
    assert mock_get_modules.await_count == 2
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON

    # A read in between, as the next module's log asks for, misses it too, but it's
    # too soon to confirm, so it's still held
    freezer.tick(timedelta(seconds=1))
    await mock_config_entry.runtime_data.module_status.async_refresh()
    await hass.async_block_till_done()
    assert mock_get_modules.await_count == 3
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON

    # Still missing from the read the full delay after the first miss, so it's off
    freezer.tick(timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_get_modules.await_count == 4
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_OFF


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_module_rejoins_before_confirming_read(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_get_modules: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test that a module back by the confirming read never shows off the bus.

    A program write re-announces the modules one at a time, and a read between two
    announcements misses the modules still to come.
    """
    entity_id = "binary_sensor.module_1_connectivity"
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    changes: list[str | None] = []

    @callback
    def record(event: Event[EventStateChangedData]) -> None:
        changes.append(state.state if (state := event.data["new_state"]) else None)

    async_track_state_change_event(hass, entity_id, record)

    mock_get_modules.return_value = []
    await mock_config_entry.runtime_data.module_status.async_refresh()
    await hass.async_block_till_done()

    mock_get_modules.return_value = [ModuleInfo(address=1, vid=3, errors=0)]
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_get_modules.await_count == 3

    assert STATE_OFF not in changes
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_module_log_newer_than_read_wins(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_vantage: Vantage,
    mock_get_modules: AsyncMock,
) -> None:
    """Test that a module log arriving mid-read wins over what that read found."""
    entity_id = "binary_sensor.module_1_connectivity"
    await setup_integration(hass, mock_config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)
    subscribe = cast(MagicMock, mock_vantage.event_stream.subscribe_system_log)

    # The module drops off while a read is under way, which still finds it
    async def read_then_log(*_args: Any, **_kwargs: Any) -> list[ModuleInfo]:
        subscribe.call_args.args[0](
            SystemLogReceived(
                SysLogType.INFORMATION, "Station", "Module Offline: 1.1 [3]"
            )
        )
        return [ModuleInfo(address=1, vid=3, errors=0)]

    mock_get_modules.side_effect = read_then_log
    await mock_config_entry.runtime_data.module_status.async_refresh()
    await hass.async_block_till_done()
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_OFF

    # The log asked for a read of its own, which finds it gone
    mock_get_modules.side_effect = None
    mock_get_modules.return_value = []
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_OFF

    # And an online log turns it back on at once, before any read finds it
    mock_get_modules.reset_mock()
    subscribe.call_args.args[0](
        SystemLogReceived(SysLogType.INFORMATION, "Station", "Module Online: 1.1 [3]")
    )
    assert (state := hass.states.get(entity_id))
    assert state.state == STATE_ON


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
