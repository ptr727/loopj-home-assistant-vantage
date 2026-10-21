"""Tests for the Vantage InFusion events."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from aiovantage import Vantage
from aiovantage.config_client import SysLogType, TriggerType
from aiovantage.events import ObjectUpdated, SystemLogReceived, TriggerReceived
from aiovantage.objects import Button
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant

from custom_components.vantage.const import (
    DOMAIN,
    EVENT_BUTTON_PRESSED,
    EVENT_BUTTON_RELEASED,
    EVENT_SYSTEM_LOG,
    EVENT_TASK_STARTED,
    EVENT_TASK_STATE_CHANGED,
    EVENT_TASK_STOPPED,
    EVENT_TRIGGER,
)
from custom_components.vantage.logbook import async_describe_events
from custom_components.vantage.module_status import ModuleStatus
from custom_components.vantage.system_log import (
    COMPILER_SOURCE,
    REPEAT_REMINDER,
    REPEAT_WINDOW,
    Shape,
    is_noise,
    log_type_name,
)
from custom_components.vantage.triggers import trigger_type_name

from .conftest import HOST

pytestmark = pytest.mark.usefixtures("init_integration")


@pytest.mark.parametrize(
    ("state", "event_type"),
    [
        (Button.State.Down, EVENT_BUTTON_PRESSED),
        (Button.State.Up, EVENT_BUTTON_RELEASED),
    ],
)
async def test_button_events(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    state: Button.State,
    event_type: str,
) -> None:
    """Test that button presses and releases fire events."""
    events = async_capture_events(hass, event_type)

    button = mock_vantage.buttons[61]
    button.state = state
    mock_vantage.buttons.emit(ObjectUpdated(button, ["state"]))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "button_id": 61,
        "button_name": "Kitchen Button",
        "button_position": 1,
        "button_text1": "All",
        "button_text2": "On",
        "station_id": 4,
        "station_name": "Kitchen Keypad",
    }


@pytest.mark.parametrize(
    ("attrs", "event_type", "data"),
    [
        (
            {"running": True},
            EVENT_TASK_STARTED,
            {"task_id": 60, "task_name": "Goodnight"},
        ),
        (
            {"running": False},
            EVENT_TASK_STOPPED,
            {"task_id": 60, "task_name": "Goodnight"},
        ),
        (
            {"state": 2},
            EVENT_TASK_STATE_CHANGED,
            {"task_id": 60, "task_name": "Goodnight", "task_state": 2},
        ),
    ],
)
async def test_task_events(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    attrs: dict[str, Any],
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Test that task changes fire events."""
    events = async_capture_events(hass, event_type)

    task = mock_vantage.tasks[60]
    for attr, value in attrs.items():
        setattr(task, attr, value)
    mock_vantage.tasks.emit(ObjectUpdated(task, list(attrs)))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == data


async def async_wait_for_events(events: list[Event[Any]], count: int) -> None:
    """Wait for the event workers, whose name lookups run outside tracked tasks."""
    for _ in range(20):
        if len(events) >= count:
            return
        await asyncio.sleep(0)


def trigger_callback(vantage: Vantage) -> Callable[[TriggerReceived], None]:
    """Return the callback the integration subscribed to triggers with."""
    subscribe = cast(MagicMock, vantage.event_stream.subscribe_triggers)
    return subscribe.call_args.args[0]


def system_log_callback(vantage: Vantage) -> Callable[[SystemLogReceived], None]:
    """Return the callback the integration subscribed to the system log with."""
    subscribe = cast(MagicMock, vantage.event_stream.subscribe_system_log)
    return subscribe.call_args.args[0]


@pytest.mark.parametrize("supports_enhanced_log", [True])
@pytest.mark.parametrize(
    ("event", "trigger_type"),
    [(TriggerType.BUTTON_DOWN, "button_down"), (99, "unknown_99")],
)
async def test_trigger_event(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    event: int,
    trigger_type: str,
) -> None:
    """Test that a button starting a task fires a trigger event."""
    events = async_capture_events(hass, EVENT_TRIGGER)

    trigger_callback(mock_vantage)(TriggerReceived(61, 60, event, 1, 0))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "host": HOST,
        "trigger_type": trigger_type,
        "event": event,
        "source_id": 61,
        "source_name": "Kitchen Button",
        "source_path": ["Kitchen Button", "Kitchen Keypad", "Controller 1"],
        "task_id": 60,
        "task_name": "Goodnight",
        "param1": 1,
        "param2": 0,
    }


@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_trigger_event_unloaded_object(
    hass: HomeAssistant,
    mock_vantage: Vantage,
) -> None:
    """Test that a trigger from an object the integration doesn't load is named."""
    events = async_capture_events(hass, EVENT_TRIGGER)
    timer = SimpleNamespace(vid=70, obj=SimpleNamespace(name="Sunset", parent=None))

    with patch(
        "custom_components.vantage.names.ConfigurationInterface.get_object",
        return_value=[timer],
    ) as get_object:
        for _ in range(2):
            trigger_callback(mock_vantage)(
                TriggerReceived(70, 60, TriggerType.TIMER, 0, 0)
            )
            await async_wait_for_events(events, len(events) + 1)

    # Looked up once, then named from the cache
    get_object.assert_awaited_once()
    assert len(events) == 2
    for event in events:
        assert event.data["source_name"] == "Sunset"
        assert event.data["source_path"] == ["Sunset"]
        assert event.data["trigger_type"] == "timer"


@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_trigger_events_in_order(
    hass: HomeAssistant,
    mock_vantage: Vantage,
) -> None:
    """Test that triggers fire in order, though the first waits on a name lookup."""
    events = async_capture_events(hass, EVENT_TRIGGER)
    answer = asyncio.Event()
    timer = SimpleNamespace(vid=70, obj=SimpleNamespace(name="Sunset", parent=None))

    async def slow_lookup(*args: Any) -> list[Any]:
        await answer.wait()
        return [timer]

    with patch(
        "custom_components.vantage.names.ConfigurationInterface.get_object",
        side_effect=slow_lookup,
    ):
        on_trigger = trigger_callback(mock_vantage)
        on_trigger(TriggerReceived(70, 60, TriggerType.TIMER, 0, 0))
        on_trigger(TriggerReceived(61, 60, TriggerType.BUTTON_DOWN, 0, 0))
        await asyncio.sleep(0)
        assert not events

        answer.set()
        await async_wait_for_events(events, 2)

    assert [event.data["source_name"] for event in events] == [
        "Sunset",
        "Kitchen Button",
    ]


@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_trigger_event_failure(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a trigger that can't be described doesn't stop later triggers."""
    events = async_capture_events(hass, EVENT_TRIGGER)

    with patch(
        "custom_components.vantage.triggers.describe_trigger",
        side_effect=[ValueError("Bad reply"), {"source_name": "Kitchen Button"}],
    ):
        on_trigger = trigger_callback(mock_vantage)
        on_trigger(TriggerReceived(70, 60, TriggerType.TIMER, 0, 0))
        on_trigger(TriggerReceived(61, 60, TriggerType.BUTTON_DOWN, 0, 0))
        await hass.async_block_till_done(wait_background_tasks=True)

    assert "Failed to handle TriggerReceived" in caplog.text
    assert [event.data["source_name"] for event in events] == ["Kitchen Button"]


@pytest.mark.parametrize(
    ("value", "trigger_type", "log_type"),
    [
        (None, "unknown", "unknown"),
        (3, "timer", "warning"),
        (99, "unknown_99", "unknown_99"),
    ],
)
def test_type_names(value: int | None, trigger_type: str, log_type: str) -> None:
    """Test naming trigger and system log types, known, unknown and missing."""
    assert trigger_type_name(value) == trigger_type
    assert log_type_name(value) == log_type


async def test_no_enhanced_log(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that firmware without the enhanced log subscribes to neither log."""
    cast(MagicMock, mock_vantage.event_stream.subscribe_triggers).assert_not_called()
    cast(MagicMock, mock_vantage.event_stream.subscribe_system_log).assert_not_called()

    # Said once at info level, on each setup
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert caplog.text.count("no enhanced log") == 1


@pytest.mark.parametrize("supports_enhanced_log", [True])
@pytest.mark.parametrize(
    ("log", "data"),
    [
        (
            SystemLogReceived(SysLogType.WARNING, "Controller", "Overload (VID:3)"),
            {
                "host": HOST,
                "type": "warning",
                "source": "Controller",
                "message": "Overload (VID:3)",
                "object_id": 3,
                "object_name": "Module 1",
            },
        ),
        (
            SystemLogReceived(SysLogType.ERROR, "Controller", "Out of memory"),
            {
                "host": HOST,
                "type": "error",
                "source": "Controller",
                "message": "Out of memory",
            },
        ),
        (
            SystemLogReceived(
                SysLogType.INFORMATION, "Station", "Module Online: 1.1 [3]"
            ),
            {
                "host": HOST,
                "type": "information",
                "source": "Station",
                "message": "Module Online: 1.1 [3]",
                "object_id": 3,
                "object_name": "Module 1",
            },
        ),
        (
            SystemLogReceived(SysLogType.WARNING, "Controller", "[ALERT] Check 1"),
            {
                "host": HOST,
                "type": "warning",
                "source": "Controller",
                "message": "[ALERT] Check 1",
            },
        ),
        (
            SystemLogReceived(42, "Station", "Module Status: 1.1 [3]"),
            {
                "host": HOST,
                "type": "unknown_42",
                "source": "Station",
                "message": "Module Status: 1.1 [3]",
                "object_id": 3,
                "object_name": "Module 1",
            },
        ),
    ],
)
async def test_system_log_event(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    log: SystemLogReceived,
    data: dict[str, Any],
) -> None:
    """Test that a notable system log fires an event."""
    events = async_capture_events(hass, EVENT_SYSTEM_LOG)

    system_log_callback(mock_vantage)(log)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == data


@pytest.mark.parametrize("supports_enhanced_log", [True])
@pytest.mark.parametrize(
    "log",
    [
        SystemLogReceived(SysLogType.INFORMATION, "Controller", "Task completed"),
        SystemLogReceived(SysLogType.TASK, "Controller", "Task 60 started"),
        SystemLogReceived(SysLogType.WARNING, COMPILER_SOURCE, "Unused variable"),
        SystemLogReceived(
            SysLogType.WARNING,
            "Controller",
            "Script 60 compiled with warnings in 0.5 seconds.",
        ),
    ],
)
async def test_system_log_not_forwarded(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    log: SystemLogReceived,
) -> None:
    """Test that routine logs and compiler output fire no event."""
    events = async_capture_events(hass, EVENT_SYSTEM_LOG)

    system_log_callback(mock_vantage)(log)
    await hass.async_block_till_done()

    assert not events


@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_system_log_burst(
    hass: HomeAssistant,
    mock_vantage: Vantage,
) -> None:
    """Test that a burst of one warning over numbered stations fires one event."""
    events = async_capture_events(hass, EVENT_SYSTEM_LOG)

    for station in range(1, 4):
        system_log_callback(mock_vantage)(
            SystemLogReceived(
                SysLogType.WARNING,
                "Controller",
                f"Station bus command not processed on station {station}",
            )
        )
    await hass.async_block_till_done()

    assert len(events) == 1


def test_system_log_burst_reminder() -> None:
    """Test that a burst passes the first warning, then one again hourly."""
    seen: dict[Shape, tuple[float, float]] = {}

    def noise(message: str, now: float) -> bool:
        log = SystemLogReceived(SysLogType.WARNING, "Controller", message)
        return is_noise(log, seen, now)

    assert not noise("Station 1 is slow", 0)
    assert noise("Station 2 is slow", 30)

    # Still in the burst an hour after the first passed, so one passes as a reminder
    now = 30.0
    while now + 30 < REPEAT_REMINDER:
        now += 30
        assert noise("Station 3 is slow", now)
    now = REPEAT_REMINDER + 10
    assert not noise("Station 4 is slow", now)
    assert noise("Station 5 is slow", now + 30)

    # A quiet spell ends the burst
    now += 30 + REPEAT_WINDOW
    assert not noise("Station 6 is slow", now)

    # Warnings naming another VID stay apart
    assert not noise("Station 7 is slow (VID:3)", now + 1)
    assert not noise("Station 7 is slow (VID:4)", now + 2)
    assert noise("Station 8 is slow (VID:4)", now + 3)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("supports_enhanced_log", [True])
async def test_system_log_module_offline(
    hass: HomeAssistant,
    mock_vantage: Vantage,
) -> None:
    """Test that a module going offline refreshes its connectivity at once."""
    assert (state := hass.states.get("binary_sensor.module_1_connectivity"))
    assert state.state == STATE_ON

    with patch(
        "custom_components.vantage.module_status.read_module_status",
        return_value=ModuleStatus(enclosures={1}),
    ):
        system_log_callback(mock_vantage)(
            SystemLogReceived(
                SysLogType.INFORMATION, "Station", "Module Offline: 1.1 [3]"
            )
        )
        await hass.async_block_till_done()

    assert (state := hass.states.get("binary_sensor.module_1_connectivity"))
    assert state.state == STATE_OFF


async def test_logbook_descriptions(hass: HomeAssistant) -> None:
    """Test that system log events, and not trigger events, are described."""
    describers: dict[str, Callable[[Event[Any]], dict[str, str]]] = {}

    def describe_event(
        domain: str, event_type: str, describer: Callable[[Event[Any]], dict[str, str]]
    ) -> None:
        assert domain == DOMAIN
        describers[event_type] = describer

    async_describe_events(hass, describe_event)

    assert EVENT_TRIGGER not in describers
    system_log = describers[EVENT_SYSTEM_LOG](
        Event(EVENT_SYSTEM_LOG, {"type": "warning", "message": "Overload (VID:3)"})
    )
    assert system_log == {
        LOGBOOK_ENTRY_NAME: "Vantage",
        LOGBOOK_ENTRY_MESSAGE: "Warning: Overload (VID:3)",
    }

    # Missing values are left out rather than rendered as "None"
    system_log = describers[EVENT_SYSTEM_LOG](
        Event(EVENT_SYSTEM_LOG, {"type": None, "message": "Overload"})
    )
    assert system_log[LOGBOOK_ENTRY_MESSAGE] == "Overload"
