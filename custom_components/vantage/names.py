"""Resolve Vantage object names, including objects the integration doesn't load."""

from typing import Any

from aiovantage import Vantage
from aiovantage.config_client import ConfigurationInterface
from aiovantage.errors import ClientError

from .const import LOGGER

# Guards against a parent loop in the controller's configuration
MAX_PATH_DEPTH = 8


class ObjectNames:
    """Resolve object names and parent chains, including objects not loaded."""

    def __init__(self, vantage: Vantage) -> None:
        """Initialize the resolver."""
        self._vantage = vantage
        self._cache: dict[int, tuple[str, int | None]] = {}

    async def path(self, vid: int) -> list[str]:
        """Return the names of an object and its parents, eg. sensor, module, enclosure."""
        names: list[str] = []
        seen: set[int] = set()
        current: int | None = vid
        while current and current not in seen and len(names) < MAX_PATH_DEPTH:
            seen.add(current)
            name, current = await self._lookup(current)
            names.append(name)

        return names

    async def name(self, vid: int) -> str:
        """Return the name of an object."""
        return (await self._lookup(vid))[0]

    async def _lookup(self, vid: int) -> tuple[str, int | None]:
        # Objects the integration loaded, eg. buttons, sensors, modules and tasks
        if vid in self._vantage:
            obj = self._vantage[vid]
            parent = getattr(obj, "parent", None)
            return obj.name or f"VID {vid}", getattr(parent, "vid", None)

        # Other objects, eg. timers and enclosures, fetched once and cached
        if vid not in self._cache:
            try:
                objects = await ConfigurationInterface.get_object(
                    self._vantage.config_client, vid
                )
            except ClientError as err:
                LOGGER.debug("Failed to look up object %d: %s", vid, err)
                return f"VID {vid}", None

            self._cache[vid] = _name_and_parent(
                vid, objects[0].obj if objects else None
            )

        return self._cache[vid]


def _name_and_parent(vid: int, obj: Any) -> tuple[str, int | None]:
    # Types aiovantage doesn't model come back as generic elements
    if hasattr(obj, "children"):
        fields = {child.qname: child.text for child in obj.children}
        parent = fields.get("Parent")
        return (
            fields.get("Name") or f"VID {vid}",
            int(parent) if parent and parent.strip().isdigit() else None,
        )

    parent = getattr(obj, "parent", None)
    return getattr(obj, "name", None) or f"VID {vid}", getattr(parent, "vid", None)
