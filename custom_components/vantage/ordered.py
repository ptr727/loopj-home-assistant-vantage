"""Handle items one at a time, in the order they arrive."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import LOGGER


class OrderedWorker[T]:
    """Handle items in arrival order, from a background task that ends when idle.

    Ending when idle, rather than waiting on a queue forever, leaves no task running
    between items, so waiting for the entry's background tasks returns once every
    item queued so far is handled.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        handle: Callable[[T], Awaitable[None]],
        name: str,
    ) -> None:
        """Initialize the worker."""
        self._hass = hass
        self._entry = entry
        self._handle = handle
        self._name = name
        self._items: deque[T] = deque()
        self._task: asyncio.Task[None] | None = None

    def put(self, item: T) -> None:
        """Queue an item, and start the worker unless it is already running."""
        self._items.append(item)
        if self._task is None or self._task.done():
            # Tied to the entry, so an unload cancels it
            self._task = self._entry.async_create_background_task(
                self._hass, self._drain(), self._name
            )

    async def _drain(self) -> None:
        while self._items:
            item = self._items.popleft()
            # One bad item must not stop the worker, or no later item would be handled
            try:
                await self._handle(item)
            except Exception:
                LOGGER.exception("Failed to handle %s", item)
