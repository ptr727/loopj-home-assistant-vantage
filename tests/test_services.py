"""Tests for the Vantage InFusion actions."""

from collections.abc import Awaitable, Callable, Generator
from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock, PropertyMock, patch

from aiovantage import Vantage
from aiovantage.config_client import (
    EventLogEntry,
    ModuleInfo,
    SysLogEntry,
    SysLogType,
    TriggerType,
)
from aiovantage.errors import ClientConnectionError, ClientResponseError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.vantage.const import (
    DOMAIN,
    SERVICE_GET_MODULE_STATUS,
    SERVICE_GET_SYSTEM_LOG,
    SERVICE_GET_TRIGGER_HISTORY,
    SERVICE_START_TASK,
    SERVICE_STOP_TASK,
)
from custom_components.vantage.diagnostic_services import format_errors
from custom_components.vantage.module_status import UNLOADED
from homeassistant.const import ATTR_ID, ATTR_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import ENCLOSURE_VID, HOST

pytestmark = pytest.mark.usefixtures("init_integration")


@pytest.mark.parametrize(
    ("service", "method"),
    [(SERVICE_START_TASK, "start"), (SERVICE_STOP_TASK, "stop")],
)
@pytest.mark.parametrize("service_data", [{ATTR_ID: 60}, {ATTR_NAME: "Goodnight"}])
async def test_task_actions(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    service: str,
    method: str,
    service_data: dict[str, Any],
) -> None:
    """Test starting and stopping a task by id or by name."""
    with patch.object(mock_vantage.tasks[60], method) as mock_method:
        await hass.services.async_call(DOMAIN, service, service_data, blocking=True)

    mock_method.assert_awaited_once_with()


async def test_task_not_found(
    hass: HomeAssistant,
    mock_vantage: Vantage,
) -> None:
    """Test starting a task the controller does not have."""
    with pytest.raises(
        ServiceValidationError,
        match='Task "Good Morning" not found in any Vantage controller',
    ):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_TASK, {ATTR_NAME: "Good Morning"}, blocking=True
        )


@pytest.mark.parametrize(
    ("service", "method", "error_match"),
    [
        (SERVICE_START_TASK, "start", 'Failed to start task "Goodnight"'),
        (SERVICE_STOP_TASK, "stop", 'Failed to stop task "Goodnight"'),
    ],
)
async def test_task_action_error(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    service: str,
    method: str,
    error_match: str,
) -> None:
    """Test a task action when the controller request fails."""
    with (
        patch.object(mock_vantage.tasks[60], method, side_effect=ClientConnectionError),
        pytest.raises(HomeAssistantError, match=error_match),
    ):
        await hass.services.async_call(DOMAIN, service, {ATTR_ID: 60}, blocking=True)


@pytest.fixture
def mock_masters() -> Generator[None]:
    """Connect to master 1, with peers 2 and 3."""
    with (
        patch.object(
            Vantage, "master_number", new_callable=PropertyMock, return_value=1
        ),
        patch.object(Vantage, "peers", new_callable=PropertyMock, return_value=[2, 3]),
    ):
        yield


def by_master(
    logs: dict[int | None, list[Any] | Exception],
) -> Callable[..., Awaitable[list[Any]]]:
    """Return a log reader answering with each master's log, or raising its error."""

    async def read(client: Any, master: int | None = None) -> list[Any]:
        log = logs[master]
        if isinstance(log, Exception):
            raise log
        return log

    return read


@pytest.mark.usefixtures("mock_masters")
async def test_get_trigger_history(hass: HomeAssistant) -> None:
    """Test reading the trigger history of each master."""
    button_press = EventLogEntry(
        time_stamp="2026-01-02T03:04:05",
        source=61,
        destination=60,
        event=TriggerType.BUTTON_DOWN,
        param1=0,
        param2=0,
    )
    logs: dict[int | None, list[Any] | Exception] = {
        None: [replace(button_press, time_stamp="2026-01-01T00:00:00"), button_press],
        2: [replace(button_press, event=99)],
        3: [],
    }

    with patch(
        "custom_components.vantage.diagnostic_services.DiagnosticInterface.get_event_log",
        side_effect=by_master(logs),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_TRIGGER_HISTORY,
            {"limit": 1},
            blocking=True,
            return_response=True,
        )

    entry = {
        "time": "2026-01-02T03:04:05",
        "host": HOST,
        "master": 1,
        "trigger_type": "button_down",
        "event": TriggerType.BUTTON_DOWN,
        "source_id": 61,
        "source_name": "Kitchen Button",
        "source_path": ["Kitchen Button", "Kitchen Keypad", "Controller 1"],
        "task_id": 60,
        "task_name": "Goodnight",
        "param1": 0,
        "param2": 0,
    }
    assert response == {
        "entries": [
            entry,
            entry | {"master": 2, "trigger_type": "unknown_99", "event": 99},
        ],
        "errors": [],
    }


@pytest.mark.usefixtures("mock_masters")
async def test_get_system_log(hass: HomeAssistant) -> None:
    """Test reading the system log, with a peer that answers with an error."""
    warning = SysLogEntry(
        type=SysLogType.WARNING, time="01-02T03:04:05", src="Controller", msg="Hot "
    )
    logs: dict[int | None, list[Any] | Exception] = {
        None: [warning],
        2: ClientResponseError("Not available"),
        3: [replace(warning, type=42, msg=None)],
    }

    with patch(
        "custom_components.vantage.diagnostic_services.DiagnosticInterface.get_sys_log",
        side_effect=by_master(logs),
    ):
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_SYSTEM_LOG, blocking=True, return_response=True
        )

    entry = {
        "time": "01-02T03:04:05",
        "host": HOST,
        "master": 1,
        "type": "warning",
        "source": "Controller",
        "message": "Hot",
    }
    assert response == {
        "entries": [
            entry,
            entry | {"master": 3, "type": "unknown_42", "message": ""},
        ],
        "errors": [{"host": HOST, "master": 2, "error": "Not available"}],
    }


@pytest.mark.usefixtures("mock_masters")
async def test_get_system_log_peer_connection_error(hass: HomeAssistant) -> None:
    """Test that a connection error stops reading, and names the masters not read."""
    logs: dict[int | None, list[Any] | Exception] = {
        None: [SysLogEntry(type=SysLogType.ERROR, time="01-02T03:04:05", msg="Down")],
        2: ClientConnectionError(),
        3: [],
    }

    with patch(
        "custom_components.vantage.diagnostic_services.DiagnosticInterface.get_sys_log",
        side_effect=by_master(logs),
    ) as get_sys_log:
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_SYSTEM_LOG, blocking=True, return_response=True
        )

    assert get_sys_log.await_count == 2
    assert response
    assert len(cast(list[Any], response["entries"])) == 1
    assert response["errors"] == [
        {"host": HOST, "master": 2, "error": "ClientConnectionError"},
        {"host": HOST, "master": 3, "error": "Not read after master 2 failed"},
    ]


@pytest.mark.usefixtures("mock_masters")
@pytest.mark.parametrize(
    ("service", "method", "error_match"),
    [
        (
            SERVICE_GET_TRIGGER_HISTORY,
            "get_event_log",
            "Failed to read the trigger history: ",
        ),
        (SERVICE_GET_SYSTEM_LOG, "get_sys_log", "Failed to read the system log: "),
    ],
)
async def test_diagnostic_action_error(
    hass: HomeAssistant,
    service: str,
    method: str,
    error_match: str,
) -> None:
    """Test that a log action fails when no master could be read."""
    with (
        patch(
            f"custom_components.vantage.diagnostic_services.DiagnosticInterface.{method}",
            side_effect=ClientConnectionError("Connection refused"),
        ),
        pytest.raises(HomeAssistantError, match=error_match) as err,
    ):
        await hass.services.async_call(
            DOMAIN, service, blocking=True, return_response=True
        )

    # The connected master failed, so the peers were not read
    assert str(err.value) == (
        f"{error_match}master 1: Connection refused; "
        "master 2: Not read after master 1 failed; "
        "master 3: Not read after master 1 failed"
    )


@pytest.mark.parametrize(
    "service", [SERVICE_GET_TRIGGER_HISTORY, SERVICE_GET_SYSTEM_LOG]
)
@pytest.mark.parametrize("limit", [0, 5001, "many"])
async def test_diagnostic_action_invalid_limit(
    hass: HomeAssistant, service: str, limit: Any
) -> None:
    """Test that a limit outside the range services.yaml offers is rejected."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, service, {"limit": limit}, blocking=True, return_response=True
        )


def test_format_errors() -> None:
    """Test that errors read as text, naming the controller when several failed."""
    assert format_errors([{"host": HOST, "master": 2, "error": "Timeout"}]) == (
        "master 2: Timeout"
    )
    assert (
        format_errors(
            [
                {"host": "192.0.2.10", "enclosure": 7, "error": "Timeout"},
                {"host": "192.0.2.11", "error": "ClientConnectionError"},
            ]
        )
        == "192.0.2.10 enclosure 7: Timeout; 192.0.2.11: ClientConnectionError"
    )


@pytest.mark.usefixtures("mock_masters")
async def test_get_trigger_history_unloaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test that the masters not yet read when the entry unloads are reported."""
    names = mock_config_entry.runtime_data.object_names

    async def unload_during_read(client: Any, master: int | None = None) -> list[Any]:
        names.cancel()
        return []

    with (
        patch(
            "custom_components.vantage.diagnostic_services.DiagnosticInterface.get_event_log",
            side_effect=unload_during_read,
        ) as get_event_log,
        pytest.raises(HomeAssistantError, match=UNLOADED),
    ):
        await hass.services.async_call(
            DOMAIN, SERVICE_GET_TRIGGER_HISTORY, blocking=True, return_response=True
        )

    get_event_log.assert_awaited_once()


@pytest.mark.parametrize(
    "service",
    [SERVICE_GET_TRIGGER_HISTORY, SERVICE_GET_SYSTEM_LOG, SERVICE_GET_MODULE_STATUS],
)
async def test_diagnostic_action_entry_not_loaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    service: str,
) -> None:
    """Test that the actions fail when no controller is loaded."""
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="No Vantage controller is loaded"):
        await hass.services.async_call(
            DOMAIN, service, blocking=True, return_response=True
        )


@pytest.mark.usefixtures("mock_masters")
async def test_get_module_status(
    hass: HomeAssistant,
    mock_vantage: Vantage,
    mock_enclosure: AsyncMock,
) -> None:
    """Test reading the modules on each enclosure's bus."""
    module = ModuleInfo(
        address=1,
        vid=3,
        model="Module",
        version="1.0",
        status=ModuleInfo.Status(proc0="Running", proc1="Running"),
        errors=2,
    )

    with patch(
        "custom_components.vantage.module_status.DiagnosticInterface.get_modules",
        return_value=[module, ModuleInfo(address=2)],
    ) as get_modules:
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_MODULE_STATUS, blocking=True, return_response=True
        )

    # The enclosure is addressed by its position, on the connected master
    get_modules.assert_awaited_once_with(mock_vantage.config_client, 2, master=None)
    assert response == {
        "modules": [
            {
                "host": HOST,
                "module_id": 3,
                "module_name": "Module 1",
                "online": True,
                "address": 1,
                "model": "Module",
                "version": "1.0",
                "status": ["Running", "Running"],
                "errors": 2,
            }
        ],
        "errors": [],
    }


@pytest.mark.usefixtures("mock_masters", "mock_enclosure")
@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (ClientConnectionError(), "ClientConnectionError"),
        (ValueError("Bad reply"), "Bad reply"),
    ],
)
async def test_get_module_status_error(
    hass: HomeAssistant,
    side_effect: Exception,
    error: str,
) -> None:
    """Test that an enclosure that can't be read is reported, its modules unknown."""
    with patch(
        "custom_components.vantage.module_status.DiagnosticInterface.get_modules",
        side_effect=side_effect,
    ):
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_MODULE_STATUS, blocking=True, return_response=True
        )

    assert response
    assert response["errors"] == [
        {"host": HOST, "enclosure": ENCLOSURE_VID, "error": error}
    ]
    modules = cast(list[dict[str, Any]], response["modules"])
    assert [module["online"] for module in modules] == [None]
