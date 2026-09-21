"""Shared test helpers for Sunplug tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.sunplug.const import CONF_INGEST_URL, CONF_TOKEN

MOCK_TOKEN = "test-token"
MOCK_INGEST_URL = "https://ingest.sunplug.app/v1/ingest/abc123"


async def async_set_energy_prefs(
    hass: HomeAssistant, sources: list[dict[str, Any]]
) -> None:
    """Store Energy dashboard preferences with the given sources."""
    manager = await async_get_manager(hass)
    await manager.async_update({"energy_sources": sources, "device_consumption": []})


def set_power_state(
    hass: HomeAssistant,
    entity_id: str,
    value: float | str,
    *,
    unit: str = "W",
    platform: str | None = None,
    last_reported: datetime | None = None,
) -> None:
    """Set a power sensor's state, optionally registering it with a platform."""
    if platform is not None:
        registry = er.async_get(hass)
        registry.async_get_or_create(
            "sensor",
            platform,
            f"unique_{entity_id}",
            suggested_object_id=entity_id.split(".", 1)[1],
        )
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


def mock_config_entry_data() -> dict[str, Any]:
    """Return typical config entry data for a paired Sunplug entry."""
    return {CONF_TOKEN: MOCK_TOKEN, CONF_INGEST_URL: MOCK_INGEST_URL}
