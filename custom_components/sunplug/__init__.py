"""The Sunplug integration.

Reads the live power sensors the user has configured in Home Assistant's
Energy dashboard and pushes readings to Sunplug's ingest endpoint. There is
no manual entity mapping: the Energy dashboard preferences are the only
source of truth for which entities represent solar production, grid power
and battery power.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import SunplugSender

type SunplugConfigEntry = ConfigEntry[SunplugSender]


async def async_setup_entry(hass: HomeAssistant, entry: SunplugConfigEntry) -> bool:
    """Set up Sunplug from a config entry."""
    sender = SunplugSender(hass, entry)
    await sender.async_setup()
    entry.runtime_data = sender
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunplugConfigEntry) -> bool:
    """Unload a Sunplug config entry."""
    await entry.runtime_data.async_unload()
    return True
