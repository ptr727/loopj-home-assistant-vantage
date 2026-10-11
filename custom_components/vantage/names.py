"""Resolve Vantage object names, including objects the integration doesn't load."""

import asyncio
from typing import Any, NamedTuple

from aiovantage import Vantage
from aiovantage.config_client import ConfigurationInterface
from aiovantage.errors import ClientError

from .const import LOGGER

# Guards against a parent loop in the controller's configuration
MAX_PATH_DEPTH = 8


class ObjectInfo(NamedTuple):
    """The name of an object, and where it sits under its parent."""

    name: str
    parent: int | None
    position: int | None


class ObjectNames:
    """Resolve object names and parent chains, including objects not loaded."""

    def __init__(self, vantage: Vantage) -> None:
        """Initialize the resolver."""
        self._vantage = vantage
        self._cache: dict[int, ObjectInfo] = {}
        self._pending: dict[int, asyncio.Task[ObjectInfo]] = {}

    def clear(self) -> None:
        """Forget looked up objects, eg. after the controller is reprogrammed."""
        self._cache.clear()

    async def path(self, vid: int) -> list[str]:
        """Return the names of an object and its parents, eg. sensor, module, enclosure."""
        names: list[str] = []
        seen: set[int] = set()
        current: int | None = vid
        while current and current not in seen and len(names) < MAX_PATH_DEPTH:
            seen.add(current)
            info = await self.info(current)
            names.append(info.name)
            current = info.parent

        return names

    async def name(self, vid: int) -> str:
        """Return the name of an object."""
        return (await self.info(vid)).name

    async def info(self, vid: int) -> ObjectInfo:
        """Return the name, parent and position of an object."""
        # Objects the integration loaded, eg. buttons, sensors, modules and tasks
        if vid in self._vantage:
            obj = self._vantage[vid]
            parent = getattr(obj, "parent", None)
            return ObjectInfo(
                obj.name or f"VID {vid}",
                getattr(parent, "vid", None),
                getattr(parent, "position", None),
            )

        # Other objects, eg. timers and enclosures, fetched once and cached
        if (info := self._cache.get(vid)) is not None:
            return info

        # Share a lookup already in flight rather than asking the controller again
        if (pending := self._pending.get(vid)) is None:
            pending = asyncio.ensure_future(self._fetch(vid))
            self._pending[vid] = pending
            pending.add_done_callback(lambda _: self._pending.pop(vid, None))

        # Shielded, so a cancelled caller doesn't cancel the lookup the others share
        return await asyncio.shield(pending)

    async def _fetch(self, vid: int) -> ObjectInfo:
        try:
            objects = await ConfigurationInterface.get_object(
                self._vantage.config_client, vid
            )
        except ClientError as err:
            # Not cached, so the next lookup tries again
            LOGGER.debug("Failed to look up object %d: %s", vid, err)
            return ObjectInfo(f"VID {vid}", None, None)
        except Exception:
            # A decode or XML error, which aiovantage doesn't wrap, would repeat, so
            # cache the fallback rather than ask the controller again each time
            LOGGER.warning(
                "Failed to read object %d, naming it by VID", vid, exc_info=True
            )
            info = ObjectInfo(f"VID {vid}", None, None)
            self._cache[vid] = info
            return info

        info = _object_info(vid, objects[0].obj if objects else None)
        self._cache[vid] = info
        return info


def _object_info(vid: int, obj: Any) -> ObjectInfo:
    # Types aiovantage doesn't model come back as generic elements
    if hasattr(obj, "children"):
        children = {child.qname: child for child in obj.children}
        name = getattr(children.get("Name"), "text", None)
        parent = children.get("Parent")
        return ObjectInfo(
            name or f"VID {vid}",
            _int(getattr(parent, "text", None)),
            _int(getattr(parent, "attributes", {}).get("Position")),
        )

    parent = getattr(obj, "parent", None)
    return ObjectInfo(
        getattr(obj, "name", None) or f"VID {vid}",
        getattr(parent, "vid", None),
        getattr(parent, "position", None),
    )


def _int(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdigit() else None
