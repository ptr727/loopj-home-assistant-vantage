"""Constants for the Vantage InFusion Controller integration."""

import logging

# Logging
LOGGER = logging.getLogger(__package__)

# Domain
DOMAIN = "vantage"

# Services
SERVICE_START_TASK = "start_task"
SERVICE_STOP_TASK = "stop_task"
SERVICE_GET_TRIGGER_HISTORY = "get_trigger_history"
SERVICE_GET_SYSTEM_LOG = "get_system_log"

# Why a diagnostic service left something unread
UNLOADED = "Not read, the integration was unloaded"

# Events
EVENT_BUTTON_PRESSED = f"{DOMAIN}_button_pressed"
EVENT_BUTTON_RELEASED = f"{DOMAIN}_button_released"
EVENT_TASK_STARTED = f"{DOMAIN}_task_started"
EVENT_TASK_STOPPED = f"{DOMAIN}_task_stopped"
EVENT_TASK_STATE_CHANGED = f"{DOMAIN}_task_state_changed"
EVENT_TRIGGER = f"{DOMAIN}_trigger"
EVENT_SYSTEM_LOG = f"{DOMAIN}_system_log"

# Climate constants
FAN_MAX = "max"
