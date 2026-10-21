"""Tests for the Vantage InFusion binary sensor platform."""

from aiovantage import Vantage
from aiovantage.events import Disconnected, Reconnected
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    snapshot_platform,
)
from syrupy.assertion import SnapshotAssertion

from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import setup_integration


@pytest.mark.parametrize("init_integration", [Platform.BINARY_SENSOR], indirect=True)
@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the binary sensor entities."""
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_controller_connectivity(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the controller sensor stays available and reports the connection."""
    await setup_integration(hass, mock_config_entry)
    assert (state := hass.states.get("binary_sensor.controller_1"))
    assert state.state == STATE_ON

    mock_vantage.event_stream.emit(Disconnected())
    await hass.async_block_till_done()
    assert (state := hass.states.get("binary_sensor.controller_1"))
    assert state.state == STATE_OFF

    mock_vantage.event_stream.emit(Reconnected())
    await hass.async_block_till_done()
    assert (state := hass.states.get("binary_sensor.controller_1"))
    assert state.state == STATE_ON
