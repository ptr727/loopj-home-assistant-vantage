"""Vantage config entry."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from aiovantage import Vantage

from .const import DOMAIN
from .names import ObjectNames

if TYPE_CHECKING:
    from .module_status import ModuleStatusCoordinator

type VantageConfigEntry = ConfigEntry[VantageData]


@dataclass
class VantageData:
    """Data for a Vantage config entry."""

    client: Vantage
    entry_id: str
    connected: bool = True
    object_names: ObjectNames = field(init=False)
    module_status: "ModuleStatusCoordinator | None" = None

    def __post_init__(self) -> None:
        """Create the name resolver for triggers."""
        self.object_names = ObjectNames(self.client)

    @property
    def signal_connection_changed(self) -> str:
        """Dispatcher signal sent when the controller connection is lost or restored."""
        return f"{DOMAIN}_connection_changed_{self.entry_id}"
